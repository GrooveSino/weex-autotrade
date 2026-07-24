"""Single owner for bound-strategy Campaign and volume-session transitions."""

from __future__ import annotations

import asyncio
from decimal import Decimal

from .campaign_contracts import CampaignJournal, CampaignRecord
from .instance_projection import optional_available_balance
from .models import AccountInstance, ExecutionLifecycleSnapshot, StrategyDirection
from .strategy_run_commands import StrategyRunCommandMixin
from .strategy_run_helpers import active_preparation, boundary_preparation, lifecycle_now_ms, view_or_none
from .strategy_run_projection import project_strategy_run_lifecycle
from .strategy_run_recovery import StrategyRunRecoveryMixin
from .strategy_run_types import LifecyclePreparation
from .vault import CredentialMaterial, CredentialVault
from .volume_contracts import TradeVolumeLedger
from .volume_sessions import SessionVolumeService


class StrategyRunLifecycleService(StrategyRunCommandMixin, StrategyRunRecoveryMixin):
    """Own operational state; audit completeness is deliberately independent."""

    _now_ms = staticmethod(lifecycle_now_ms)
    _view_or_none = staticmethod(view_or_none)

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

    async def prepare(
        self,
        instance: AccountInstance,
        material: CredentialMaterial | None,
        direction: StrategyDirection = StrategyDirection.BTC_LONG_ETH_SHORT,
    ) -> LifecyclePreparation:
        if material is None:
            return LifecyclePreparation("unavailable", reason_code="credentials_unavailable", message="账号凭据不可用")
        active = self._journal.active_for_instance(instance.id)
        record = self._journal.get(active.campaign_id) if active is not None else self.latest_bound_record(instance.id)
        if self._manager.has_active_worker(instance.id):
            return active_preparation(record)
        if record is not None and record.status == "planned":
            stale = (
                lifecycle_now_ms() >= record.campaign.expires_at_ms
                or record.metadata.get("strategy_id") != instance.strategy_id
                or record.metadata.get("strategy_version") != instance.strategy.version
                or record.campaign.schema_version < 5
                or record.campaign.direction != direction.value
                or record.campaign.leverage != 400
                or record.campaign.margin_mode != "cross"
            )
            if stale:
                self._journal.update(
                    record.campaign_id,
                    status="stopped",
                    finished_at_ms=lifecycle_now_ms(),
                    reason="launch_preview_stale",
                )
                record = None
            else:
                return self.prepare_planned(instance, material, record)
        if record is not None and record.status in {"executing", "stopping"}:
            return active_preparation(record)
        session = self._ledger.active_session(instance.id, instance.mode.value)
        needs_recovery = (record is not None and record.status in {"recovering", "uncertain"}) or (
            session is not None and str(session.get("status")) == "recovering"
        )
        if needs_recovery:
            return await self._recover(instance, material, record, session)
        if (
            record is not None
            and record.status == "stopped"
            and str(record.metadata.get("reason") or "").startswith("launch_aborted:")
        ):
            self._finish_launch_aborted(record, session)
        try:
            boundary = await asyncio.to_thread(self._manager.inspect_bound_strategy_boundary, material)
        except Exception as exc:  # a read-only boundary failure is retryable and never creates a run
            return LifecyclePreparation(
                "unavailable",
                reason_code=f"boundary_unavailable:{type(exc).__name__.lower()}",
                message="账户持仓与挂单边界暂时不可用，请重试",
            )
        self._journal.replace_boundary_projection(instance.id, boundary)
        if not bool(boundary["flat"]):
            return boundary_preparation(boundary, instance.id)
        return LifecyclePreparation("idle", boundary=boundary)

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
            direction=str(metadata.get("direction") or record.campaign.direction),
            target_mode=str(metadata.get("target_mode") or "incremental"),
            strategy_target_quote_volume=Decimal(str(metadata.get("strategy_target_quote") or target)),
            baseline_lifetime_quote_volume=Decimal(str(metadata.get("baseline_lifetime_quote") or "0")),
            starting_available_balance_quote=optional_available_balance(
                metadata.get("starting_available_balance_quote")
            ),
        )

    def projection(self, instance_id: str, mode: str) -> ExecutionLifecycleSnapshot:
        return project_strategy_run_lifecycle(self._journal, self._ledger, instance_id, mode)

    def _finish_launch_aborted(self, record: CampaignRecord, session: dict[str, object] | None) -> None:
        now_ms = lifecycle_now_ms()
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
            finished_at_ms=lifecycle_now_ms(),
            uncertain_order_state=False,
            pending_sync=False,
        )
