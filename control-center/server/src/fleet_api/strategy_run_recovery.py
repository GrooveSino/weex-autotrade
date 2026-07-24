"""Read-only recovery convergence for bound strategy runs."""

from __future__ import annotations

import asyncio
from decimal import Decimal

from .campaign_contracts import CampaignRecord
from .campaign_events import submission_attempted
from .execution_recovery import boundary_state, recovery_due, recovery_metadata
from .instance_projection import optional_available_balance
from .models import AccountInstance
from .strategy_run_helpers import boundary_preparation, lifecycle_now_ms
from .strategy_run_types import LifecyclePreparation
from .vault import CredentialMaterial
from .volume_contracts import FillConflictError


class StrategyRunRecoveryMixin:
    async def recover_after_worker(self, record: CampaignRecord) -> None:
        instance = self._control_service.get_instance(record.instance_id)
        await self._recover(
            instance,
            self._vault.get(record.instance_id),
            record,
            self._ledger.active_session(record.instance_id, instance.mode.value),
        )

    def due_recovery_records(self, now_ms: int | None = None) -> list[CampaignRecord]:
        current_ms = lifecycle_now_ms() if now_ms is None else now_ms
        return [
            record
            for record in self._journal.list_all()
            if record.metadata.get("execution_kind") == "bound_strategy"
            and recovery_due(record, current_ms)
            and not self._manager.has_active_worker(record.instance_id)
        ]

    async def finalize_record(self, record: CampaignRecord) -> None:
        session_id = record.metadata.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            return
        session = self._ledger.get_session(session_id)
        if session is None:
            return
        if record.status in {"recovering", "uncertain"}:
            self._sessions.mark_recovering(
                session_id,
                reason=str(record.metadata.get("reason") or "campaign_outcome_uncertain"),
                finished_at_ms=int(record.metadata.get("finished_at_ms") or lifecycle_now_ms()),
            )
            await self.recover_after_worker(record)
            return
        if record.status not in {"completed", "stopped"}:
            return
        if record.status == "stopped" and not submission_attempted(record):
            self._finish_launch_aborted(record, self._ledger.session_projection(session_id))
            return
        finished_at_ms = int(record.metadata.get("finished_at_ms") or lifecycle_now_ms())
        ending_balance = optional_available_balance(record.metadata.get("ending_available_balance_quote"))
        self._ledger.update_session(
            session_id,
            status=record.status,
            audit_status="pending",
            result=record.status,
            result_reason=record.metadata.get("reason"),
            finished_at_ms=finished_at_ms,
            source_complete=False,
            stale=True,
            pending_sync=True,
            ending_available_balance_quote=ending_balance,
        )
        try:
            fills, complete, reason = await self._runtime.authoritative_session_fills(
                record.instance_id,
                session.started_at_ms,
                finished_at_ms,
            )
            if not complete:
                self._ledger.update_session(
                    session_id,
                    audit_status="pending",
                    pending_sync=False,
                    result_reason=f"session_source_incomplete:{reason}"[:160],
                )
                return
            self._ledger.record_account_fills(record.instance_id, session.mode, fills)
            projection = self._sessions.reconcile(session_id, fills, reconciled_at_ms=finished_at_ms)
            aggregate = self._ledger.aggregate(record.instance_id, 0)
            if bool(projection["reconciliation_required"]):
                self._ledger.update_session(
                    session_id,
                    status=record.status,
                    audit_status="discrepant",
                    result=record.status,
                    finished_at_ms=finished_at_ms,
                    final_lifetime_quote_volume=aggregate.lifetime,
                    ending_available_balance_quote=ending_balance,
                )
                return
            self._sessions.finalize(
                session_id,
                result=record.status,
                reason=str(record.metadata.get("reason")) if record.metadata.get("reason") else None,
                finished_at_ms=finished_at_ms,
                final_lifetime_quote_volume=aggregate.lifetime,
                ending_available_balance_quote=ending_balance,
            )
        except Exception as exc:
            self._ledger.update_session(
                session_id,
                status=record.status,
                audit_status="pending",
                pending_sync=False,
                result_reason=f"session_audit_failed:{type(exc).__name__.lower()}",
            )

    async def _recover(
        self,
        instance: AccountInstance,
        material: CredentialMaterial | None,
        record: CampaignRecord | None,
        session: dict[str, object] | None,
    ) -> LifecyclePreparation:
        now_ms = lifecycle_now_ms()
        if record is not None:
            self._journal.update(
                record.campaign_id,
                recovery_state="checking",
                last_recovery_check_at_ms=now_ms,
                next_recovery_check_at_ms=None,
            )
        if material is None:
            if record is not None:
                self._schedule_recovery(record, "waiting_read", "unknown", "credentials_unavailable")
            return LifecyclePreparation("unavailable", reason_code="credentials_unavailable", message="账号凭据不可用")
        try:
            boundary = await asyncio.to_thread(self._manager.inspect_bound_strategy_boundary, material)
        except Exception as exc:
            if record is not None:
                self._schedule_recovery(
                    record,
                    "waiting_read",
                    "unknown",
                    f"boundary_unavailable:{type(exc).__name__.lower()}",
                )
            return LifecyclePreparation(
                "unavailable",
                reason_code=f"boundary_unavailable:{type(exc).__name__.lower()}",
                message="账户持仓与挂单边界暂时不可用，请重试",
            )
        self._journal.replace_boundary_projection(instance.id, boundary)
        if not bool(boundary["flat"]):
            return self._recover_nonflat(instance, record, boundary)
        if record is not None:
            self._journal.update(
                record.campaign_id,
                recovery_state="complete",
                recovery_boundary_state="flat",
                last_recovery_check_at_ms=lifecycle_now_ms(),
                next_recovery_check_at_ms=None,
                recovery_reason=None,
            )
        return await self._recover_flat(instance, record, session, boundary)

    def _recover_nonflat(
        self,
        instance: AccountInstance,
        record: CampaignRecord | None,
        boundary: dict[str, object],
    ) -> LifecyclePreparation:
        if record is not None:
            current = self._journal.get(record.campaign_id) or record
            state = boundary_state(current, boundary)
            self._schedule_recovery(
                current,
                "cleanup_required" if state == "owned_exposure" else "waiting_boundary",
                state,
                "execution_exposure_open" if state == "owned_exposure" else "account_boundary_not_flat",
            )
            if state == "owned_exposure":
                return LifecyclePreparation(
                    "recovery_cleanup_required",
                    execution=self._view_or_none(current),
                    reason_code="execution_exposure_open",
                    message="当前任务仓位尚未收尾，可使用原停止确认短语执行安全收尾",
                    position_count=int(boundary.get("position_count") or 0),
                    regular_order_count=int(boundary.get("regular_order_count") or 0),
                    trigger_order_count=int(boundary.get("trigger_order_count") or 0),
                    blocking_positions=tuple(boundary.get("blocking_positions") or ()),
                    allowed_actions=("safe_stop", "recheck"),
                    boundary_checked_at_ms=int(boundary.get("checked_at_ms") or lifecycle_now_ms()),
                )
        return boundary_preparation(boundary, instance.id)

    async def _recover_flat(
        self,
        instance: AccountInstance,
        record: CampaignRecord | None,
        session: dict[str, object] | None,
        boundary: dict[str, object],
    ) -> LifecyclePreparation:
        if record is None:
            if session is not None:
                self._close_audit_pending_session(session, "recovery_record_missing")
            return LifecyclePreparation("idle")
        if not submission_attempted(record):
            self._finish_launch_aborted(record, session)
            return LifecyclePreparation("idle")
        if session is None:
            self._manager.archive_bound_strategy_recovery(record, recovered_at_ms=lifecycle_now_ms())
            return LifecyclePreparation("idle")
        now_ms = lifecycle_now_ms()
        fills, complete, reason = await self._runtime.authoritative_session_fills(
            instance.id,
            int(session["started_at_ms"]),
            now_ms,
        )
        if not complete:
            self._close_audit_pending_session(session, f"recovery_source_incomplete:{reason}")
            self._manager.archive_bound_strategy_recovery(record, recovered_at_ms=now_ms)
            return LifecyclePreparation("idle")
        try:
            projection = self._sessions.recover_stopped(
                str(session["session_id"]),
                fills,
                reconciled_at_ms=now_ms,
                ending_available_balance_quote=Decimal(str(boundary["available_quote"])),
            )
        except FillConflictError:
            self._close_audit_pending_session(session, "fill_conflict", discrepant=True)
        else:
            if bool(projection["reconciliation_required"]):
                self._close_audit_pending_session(session, "fill_discrepancy", discrepant=True)
        self._manager.archive_bound_strategy_recovery(record, recovered_at_ms=now_ms)
        return LifecyclePreparation("idle")

    def _schedule_recovery(self, record: CampaignRecord, state: str, boundary: str, reason: str) -> None:
        current = self._journal.get(record.campaign_id) or record
        self._journal.update(
            record.campaign_id,
            **recovery_metadata(
                current,
                now_ms=lifecycle_now_ms(),
                state=state,
                boundary=boundary,
                reason=reason,
            ),
        )
