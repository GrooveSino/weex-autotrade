"""Converge recoverable historical state before creating a new strategy run."""

from __future__ import annotations

import time
from decimal import Decimal

from .campaign_contracts import CampaignJournal, CampaignRecord
from .models import AccountInstance
from .service import UnsafeOperation
from .vault import CredentialMaterial
from .volume_contracts import FillConflictError, TradeVolumeLedger
from .volume_sessions import SessionVolumeService


class BoundStrategyRecoveryService:
    """One start-time lifecycle gate shared by every bound-strategy launch."""

    def __init__(
        self,
        ledger: TradeVolumeLedger,
        sessions: SessionVolumeService,
        runtime: object,
        campaign_manager: object,
        campaign_journal: CampaignJournal,
    ) -> None:
        self._ledger = ledger
        self._sessions = sessions
        self._runtime = runtime
        self._campaign_manager = campaign_manager
        self._campaign_journal = campaign_journal

    async def prepare_for_new_run(
        self,
        instance: AccountInstance,
        material: CredentialMaterial | None,
    ) -> bool:
        if material is None:
            raise UnsafeOperation("账号凭据不可用，无法准备实盘策略")
        session = self._ledger.active_session(instance.id, instance.mode.value)
        record = self._recovery_record(instance.id, session)
        if session is None and record is None:
            return False
        if self._campaign_manager.has_active_worker(instance.id):
            raise UnsafeOperation("当前策略执行器仍在运行，不能创建重复任务")
        if session is not None:
            status = str(session.get("status") or "verification_pending")
            if status == "stopping":
                raise UnsafeOperation("旧任务正在安全停止，请等待撤单、持仓和成交核验完成")
            if status not in {"uncertain", "verification_pending"}:
                raise UnsafeOperation("当前账号已有正在执行的策略任务")
        if record is not None and record.status in {"executing", "stopping"}:
            raise UnsafeOperation("旧任务仍在执行或停止中，不能创建新任务")

        ending_balance = self._campaign_manager.verify_bound_strategy_recovery(record, material)
        now_ms = time.time_ns() // 1_000_000
        start_ms = self._recovery_start_ms(record, session)
        fills, complete, reason = await self._runtime.authoritative_session_fills(
            instance.id,
            start_ms,
            now_ms,
        )
        if not complete:
            raise UnsafeOperation(f"旧任务成交历史尚未完整返回，暂不能启动：{reason}")

        if session is None:
            self._ledger.record_account_fills(instance.id, instance.mode.value, fills)
        else:
            try:
                projection = self._sessions.recover_stopped(
                    str(session["session_id"]),
                    fills,
                    reconciled_at_ms=now_ms,
                    ending_available_balance_quote=ending_balance,
                )
            except FillConflictError as exc:
                raise UnsafeOperation("旧任务成交账本存在冲突，无法自动确认") from exc
            if projection["status"] != "stopped" or projection["reconciliation_required"]:
                raise UnsafeOperation("旧任务成交账本与权威成交不一致，无法自动确认")

        if record is not None:
            self._campaign_manager.archive_bound_strategy_recovery(record, recovered_at_ms=now_ms)
        return True

    def _recovery_record(
        self,
        instance_id: str,
        session: dict[str, object] | None,
    ) -> CampaignRecord | None:
        records = [
            record
            for record in self._campaign_journal.list_for_instance(instance_id)
            if record.metadata.get("execution_kind") == "bound_strategy"
        ]
        if session is not None:
            session_id = str(session["session_id"])
            matched = [record for record in records if record.metadata.get("session_id") == session_id]
            if matched:
                return max(matched, key=lambda item: item.campaign.created_at_ms)
        unresolved = [
            record
            for record in records
            if record.status == "uncertain"
            and record.metadata.get("reconciliation_acknowledged_at_ms") is None
        ]
        return max(unresolved, key=lambda item: item.campaign.created_at_ms) if unresolved else None

    @staticmethod
    def _recovery_start_ms(
        record: CampaignRecord | None,
        session: dict[str, object] | None,
    ) -> int:
        if session is not None:
            return int(session["started_at_ms"])
        if record is None:
            raise RuntimeError("recovery state disappeared")
        return int(record.metadata.get("started_at_ms") or record.campaign.created_at_ms)
