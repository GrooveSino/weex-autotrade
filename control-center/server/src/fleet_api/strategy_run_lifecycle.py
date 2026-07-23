"""Single owner for bound-strategy Campaign and volume-session transitions."""

from __future__ import annotations

import time
from decimal import Decimal

from .campaign_contracts import CampaignJournal, CampaignRecord
from .campaign_events import _view, submission_attempted
from .campaign_helpers import _cleanup_confirmation
from .instance_projection import optional_available_balance
from .models import AccountInstance, BetaCampaignView, ExecutionLifecycleSnapshot
from .strategy_run_commands import StrategyRunCommandMixin
from .strategy_run_projection import project_strategy_run_lifecycle
from .strategy_run_types import LifecycleDisposition, LifecyclePreparation
from .vault import CredentialMaterial, CredentialVault
from .volume_contracts import FillConflictError, TradeVolumeLedger
from .volume_sessions import SessionVolumeService


class StrategyRunLifecycleService(StrategyRunCommandMixin):
    """Own operational state; audit completeness is deliberately independent."""

    def __init__(
        self,
        ledger: TradeVolumeLedger,
        sessions: SessionVolumeService,
        runtime: object,
        manager: object,
        journal: CampaignJournal,
        control_service: object,
        vault: CredentialVault,
    ) -> None:
        self._ledger = ledger
        self._sessions = sessions
        self._runtime = runtime
        self._manager = manager
        self._journal = journal
        self._control_service = control_service
        self._vault = vault

    def latest_bound_record(self, instance_id: str) -> CampaignRecord | None:
        records = [
            item
            for item in self._journal.list_for_instance(instance_id)
            if item.metadata.get("execution_kind") == "bound_strategy"
        ]
        return max(records, key=lambda item: item.campaign.created_at_ms) if records else None

    async def prepare(self, instance: AccountInstance, material: CredentialMaterial | None) -> LifecyclePreparation:
        if material is None:
            return LifecyclePreparation("unavailable", reason_code="credentials_unavailable", message="账号凭据不可用")
        active = self._journal.active_for_instance(instance.id)
        record = (
            self._journal.get(active.campaign_id)
            if active is not None
            else self.latest_bound_record(instance.id)
        )
        if self._manager.has_active_worker(instance.id):
            return self._active_preparation(record)
        if record is not None and record.status == "planned":
            stale = (
                self._now_ms() >= record.campaign.expires_at_ms
                or record.metadata.get("strategy_id") != instance.strategy_id
                or record.metadata.get("strategy_version") != instance.strategy.version
            )
            if stale:
                self._journal.update(
                    record.campaign_id,
                    status="stopped",
                    finished_at_ms=self._now_ms(),
                    reason="launch_preview_stale",
                )
                record = None
            else:
                return self.prepare_planned(instance, material, record)
        if record is not None and record.status in {"executing", "stopping"}:
            return self._active_preparation(record)
        session = self._ledger.active_session(instance.id, instance.mode.value)
        needs_recovery = (
            record is not None and record.status in {"recovering", "uncertain"}
        ) or (session is not None and str(session.get("status")) == "recovering")
        if needs_recovery:
            return await self._recover(instance, material, record, session)
        if record is not None and record.status == "stopped" and str(record.metadata.get("reason") or "").startswith(
            "launch_aborted:"
        ):
            self._finish_launch_aborted(record, session)
        try:
            boundary = self._manager.inspect_bound_strategy_boundary(material)
        except Exception as exc:  # a read-only boundary failure is retryable and never creates a run
            return LifecyclePreparation(
                "unavailable",
                reason_code=f"boundary_unavailable:{type(exc).__name__.lower()}",
                message="账户持仓与挂单边界暂时不可用，请重试",
            )
        if not bool(boundary["flat"]):
            cleanup_record = self._manager.prepare_bound_strategy_cleanup(instance, material, boundary)
            counts = self._boundary_counts(boundary)
            return LifecyclePreparation(
                "cleanup_required",
                execution=self._view_or_none(cleanup_record),
                cleanup_confirmation=(
                    _cleanup_confirmation(cleanup_record.campaign_id) if cleanup_record is not None else None
                ),
                **counts,
            )
        return LifecyclePreparation("idle")

    async def recover_after_worker(self, record: CampaignRecord) -> None:
        instance = self._control_service.get_instance(record.instance_id)
        await self._recover(
            instance,
            self._vault.get(record.instance_id),
            record,
            self._ledger.active_session(record.instance_id, instance.mode.value),
        )

    async def finalize_record(self, record: CampaignRecord) -> None:
        """Converge one terminal/recovering Campaign into its volume session."""
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
                finished_at_ms=int(record.metadata.get("finished_at_ms") or self._now_ms()),
            )
            await self.recover_after_worker(record)
            return
        if record.status not in {"completed", "stopped"}:
            return
        if record.status == "stopped" and not submission_attempted(record):
            self._finish_launch_aborted(record, self._ledger.session_projection(session_id))
            return
        finished_at_ms = int(record.metadata.get("finished_at_ms") or self._now_ms())
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
        except Exception as exc:  # audit failure cannot reopen or block the operational run
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
        if material is None:
            return LifecyclePreparation("unavailable", reason_code="credentials_unavailable", message="账号凭据不可用")
        boundary = self._manager.inspect_bound_strategy_boundary(material)
        if not bool(boundary["flat"]):
            counts = self._boundary_counts(boundary)
            if record is not None:
                self._journal.update(record.campaign_id, cleanup_required=True, **counts)
            return LifecyclePreparation(
                "cleanup_required",
                execution=self._view_or_none(record),
                cleanup_confirmation=_cleanup_confirmation(record.campaign_id) if record is not None else None,
                **counts,
            )

        if record is None:
            if session is not None:
                self._close_audit_pending_session(session, "recovery_record_missing")
            return LifecyclePreparation("idle")
        if not submission_attempted(record):
            self._finish_launch_aborted(record, session)
            return LifecyclePreparation("idle")

        if session is None:
            self._manager.archive_bound_strategy_recovery(record, recovered_at_ms=self._now_ms())
            return LifecyclePreparation("idle")
        now_ms = self._now_ms()
        fills, complete, reason = await self._runtime.authoritative_session_fills(
            instance.id,
            int(session["started_at_ms"]),
            now_ms,
        )
        if not complete:
            self._ledger.update_session(
                str(session["session_id"]),
                status="recovering",
                audit_status="pending",
                result_reason=f"recovery_source_incomplete:{reason}"[:160],
                pending_sync=False,
            )
            return LifecyclePreparation("recovering", execution=self._view_or_none(record), reason_code=str(reason))
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

    def establish_session(self, record: CampaignRecord, started_at_ms: int) -> None:
        metadata = record.metadata
        session_id = metadata.get("session_id")
        if metadata.get("execution_kind") != "bound_strategy" or not isinstance(session_id, str) or not session_id:
            return
        if self._ledger.get_session(session_id) is not None:
            return
        target = Decimal(str(metadata.get("session_target_quote") or record.campaign.target_turnover_quote))
        self._sessions.start(
            session_id=session_id,
            account_id=record.instance_id,
            mode="live",
            started_at_ms=started_at_ms,
            target_quote_volume=target,
            maker_only_required=True,
            strategy_id=str(metadata.get("strategy_id")) if metadata.get("strategy_id") else None,
            strategy_name=str(metadata.get("strategy_name")) if metadata.get("strategy_name") else None,
            strategy_version=int(metadata["strategy_version"]) if metadata.get("strategy_version") else None,
            target_mode=str(metadata.get("target_mode") or "incremental"),
            strategy_target_quote_volume=Decimal(str(metadata.get("strategy_target_quote") or target)),
            baseline_lifetime_quote_volume=Decimal(str(metadata.get("baseline_lifetime_quote") or "0")),
            starting_available_balance_quote=optional_available_balance(metadata.get("starting_available_balance_quote")),
        )

    def projection(self, instance_id: str, mode: str) -> ExecutionLifecycleSnapshot:
        return project_strategy_run_lifecycle(self._journal, self._ledger, instance_id, mode)

    def _finish_launch_aborted(self, record: CampaignRecord, session: dict[str, object] | None) -> None:
        now_ms = self._now_ms()
        if session is not None:
            self._ledger.update_session(
                str(session["session_id"]),
                status="stopped",
                audit_status="verified",
                result="stopped",
                result_reason=str(record.metadata.get("reason") or "launch_aborted"),
                finished_at_ms=now_ms,
                source_complete=True,
                stale=False,
                reconciliation_required=False,
                discrepancy_quote_volume=Decimal(0),
                pending_sync=False,
                uncertain_order_state=False,
            )
        if record.status != "stopped":
            self._journal.update(record.campaign_id, status="stopped", finished_at_ms=now_ms)

    def _close_audit_pending_session(
        self,
        session: dict[str, object],
        reason: str,
        *,
        discrepant: bool = False,
    ) -> None:
        self._ledger.update_session(
            str(session["session_id"]),
            status="stopped",
            audit_status="discrepant" if discrepant else "pending",
            result="stopped",
            result_reason=reason,
            finished_at_ms=self._now_ms(),
            uncertain_order_state=False,
            pending_sync=False,
        )

    @staticmethod
    def _boundary_counts(boundary: dict[str, object]) -> dict[str, int]:
        return {
            "position_count": int(boundary.get("position_count") or 0),
            "regular_order_count": int(boundary.get("regular_order_count") or 0),
            "trigger_order_count": int(boundary.get("trigger_order_count") or 0),
        }

    @staticmethod
    def _active_preparation(record: CampaignRecord | None) -> LifecyclePreparation:
        if record is None:
            return LifecyclePreparation("running")
        disposition: LifecycleDisposition = "stopping" if record.status == "stopping" else "running"
        return LifecyclePreparation(disposition, execution=_view(record, include_events=False))

    @staticmethod
    def _view_or_none(record: CampaignRecord | None) -> BetaCampaignView | None:
        return _view(record, include_events=False) if record is not None else None

    @staticmethod
    def _now_ms() -> int:
        return time.time_ns() // 1_000_000
