"""Campaign-to-session lifecycle projection for the Fleet executor."""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal

from .instance_projection import optional_available_balance
from .main_context import FleetAppContext
from .volume_history import SessionVolumeService


def install_campaign_lifecycle(ctx: FleetAppContext) -> None:
    def latest_bound_record(instance_id: str):
        records = [
            record for record in ctx.campaign_journal.list_for_instance(instance_id) if record.metadata.get("strategy_id")
        ]
        return max(records, key=lambda item: item.campaign.created_at_ms) if records else None

    async def finalize_bound_strategy_session(record) -> None:
        metadata = record.metadata
        session_id = metadata.get("session_id")
        if not isinstance(session_id, str) or not session_id or session_id in ctx.session_finalizations:
            return
        session = ctx.volume_ledger.get_session(session_id)
        if session is None:
            return
        ending_available_balance = optional_available_balance(
            metadata.get("ending_available_balance_quote")
        )
        if ending_available_balance is not None:
            ctx.volume_ledger.update_session(
                session_id,
                ending_available_balance_quote=ending_available_balance,
            )
        if session.status in {"completed", "stopped"} and session.source_complete and not session.stale:
            return
        terminal_status = str(record.status)
        finished_at_ms = int(metadata.get("finished_at_ms") or time.time_ns() // 1_000_000)
        if terminal_status == "uncertain":
            ctx.session_volume.mark_uncertain(
                session_id,
                reason=str(metadata.get("reason") or "campaign_outcome_uncertain"),
                finished_at_ms=finished_at_ms,
            )
            await ctx.publish_snapshot()
            return
        if terminal_status not in {"completed", "stopped"}:
            return

        ctx.session_finalizations.add(session_id)
        ctx.volume_ledger.update_session(
            session_id,
            status="verification_pending",
            result=terminal_status,
            result_reason=metadata.get("reason"),
            finished_at_ms=finished_at_ms,
            source_complete=False,
            stale=True,
            pending_sync=True,
        )
        try:
            fills, complete, reason = await ctx.runtime.authoritative_session_fills(
                record.instance_id,
                session.started_at_ms,
                finished_at_ms,
            )
            if not complete:
                ctx.volume_ledger.update_session(
                    session_id,
                    status="verification_pending",
                    source_complete=False,
                    stale=True,
                    reconciliation_required=True,
                    pending_sync=False,
                    result_reason=f"session_source_incomplete:{reason}"[:160],
                )
                return
            ctx.volume_ledger.record_account_fills(record.instance_id, session.mode, fills)
            projection = ctx.session_volume.reconcile(session_id, fills, reconciled_at_ms=finished_at_ms)
            if projection["reconciliation_required"]:
                return
            aggregate = ctx.volume_ledger.aggregate(record.instance_id, 0)
            ctx.session_volume.finalize(
                session_id,
                result=terminal_status,
                reason=str(metadata.get("reason")) if metadata.get("reason") else None,
                finished_at_ms=finished_at_ms,
                final_lifetime_quote_volume=aggregate.lifetime,
                ending_available_balance_quote=ending_available_balance,
            )
        except Exception as exc:
            ctx.volume_ledger.update_session(
                session_id,
                status="verification_pending",
                source_complete=False,
                stale=True,
                reconciliation_required=True,
                pending_sync=False,
                result_reason=f"session_reconciliation_failed:{type(exc).__name__.lower()}",
            )
        finally:
            ctx.session_finalizations.discard(session_id)
            await ctx.publish_snapshot()

    def schedule_session_finalization(record) -> None:
        task = asyncio.create_task(
            finalize_bound_strategy_session(record),
            name=f"fleet-session-finalize-{record.campaign_id}",
        )
        ctx.session_finalization_tasks.add(task)
        task.add_done_callback(ctx.session_finalization_tasks.discard)

    def notify_campaign_change(_instance_id: str) -> None:
        # The executor owns the Campaign lifecycle; this only projects its
        # durable state into the account list, without submitting any command.
        try:
            record = latest_bound_record(_instance_id)
            if record is not None:
                bound = ctx.campaign_manager.get(_instance_id, record.campaign_id)
                ctx.service.project_bound_strategy_execution(_instance_id, bound.status.value, bound.reason)
                session_id = record.metadata.get("session_id")
                if isinstance(session_id, str) and ctx.volume_ledger.get_session(session_id) is not None:
                    if bound.status.value == "stopping":
                        ctx.volume_ledger.update_session(session_id, status="stopping")
                    elif bound.status.value == "uncertain":
                        ctx.volume_ledger.update_session(
                            session_id,
                            status="uncertain",
                            uncertain_order_state=True,
                            stale=True,
                            reconciliation_required=True,
                        )
        except Exception:
            pass
        if ctx.event_loop is None or not ctx.event_loop.is_running():
            return

        def schedule() -> None:
            record = latest_bound_record(_instance_id)
            if record is not None and record.status in {"completed", "stopped", "uncertain"}:
                schedule_session_finalization(record)
            asyncio.create_task(ctx.publish_snapshot())

        ctx.event_loop.call_soon_threadsafe(schedule)

    def establish_bound_strategy_session(record, started_at_ms: int) -> None:
        """Create the session immediately before worker submission, never on preview."""
        metadata = record.metadata
        if metadata.get("execution_kind") != "bound_strategy":
            return
        session_id = metadata.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            return
        if ctx.volume_ledger.get_session(session_id) is not None:
            return
        target = Decimal(str(metadata.get("session_target_quote") or record.campaign.target_turnover_quote))
        SessionVolumeService(ctx.volume_ledger).start(
            session_id=session_id,
            account_id=record.instance_id,
            mode="live",
            started_at_ms=started_at_ms,
            target_quote_volume=target,
            maker_only_required=True,
            strategy_id=str(metadata.get("strategy_id")) if metadata.get("strategy_id") else None,
            strategy_name=str(metadata.get("strategy_name")) if metadata.get("strategy_name") else None,
            strategy_version=int(metadata["strategy_version"])
            if metadata.get("strategy_version") is not None
            else None,
            target_mode=str(metadata.get("target_mode") or "incremental"),
            strategy_target_quote_volume=Decimal(str(metadata.get("strategy_target_quote") or target)),
            baseline_lifetime_quote_volume=Decimal(str(metadata.get("baseline_lifetime_quote") or "0")),
            starting_available_balance_quote=optional_available_balance(
                metadata.get("starting_available_balance_quote")
            ),
        )

    ctx.latest_bound_record = latest_bound_record
    ctx.finalize_bound_strategy_session = finalize_bound_strategy_session
    ctx.schedule_session_finalization = schedule_session_finalization
    ctx.notify_campaign_change = notify_campaign_change
    ctx.establish_bound_strategy_session = establish_bound_strategy_session
