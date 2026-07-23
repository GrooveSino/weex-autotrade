from __future__ import annotations

import time
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from pydantic import SecretStr

from .campaign_log import campaign_event_log
from .execution import CycleExecutionStatus, ExecutionRecord, PositionCloseExecutionResult
from .funding import funding_preflight
from .models import (
    AccountInstance, CreateInstanceRequest, CycleSnapshot, ExposureSnapshot, FundingPreflightStatus,
    InstanceAction, InstanceStatus, LogBatch, LogLevel, LogLine, ProxySnapshot, ProxyStatus, ProxyType,
    RuntimeHealthSnapshot, StrategyProgress, StrategyStage, StrategyTargetMode, UpdateInstanceRequest,
    VolumeSnapshot, VolumeStrategy, VolumeStrategyInput, WalletSnapshot, default_volume_strategy,
)
from .ownership import LEGACY_OWNER_USER_ID, current_owner_user_id
from .proxy import ProxyValidationError, normalize_proxy_url, proxy_host
from .repository import AccountRepository
from .service_errors import BetaSourceUnavailable, InstanceNotFound, StrategyNotFound, TelemetryUnavailable, UnsafeOperation, ValidationFailed
from .service_shared import delay_label as _delay_label, now as _now
from .strategy import estimate_rounds, target_progress_quote
from .telemetry import AccountTelemetry
from .vault import CredentialMaterial, CredentialVault
from .volume_history import TradeVolumeAggregate


class ServiceExecutionMixin:
    def project_bound_strategy_execution(
        self,
        instance_id: str,
        campaign_status: str,
        reason: str | None = None,
    ) -> AccountInstance:
        """Project executor-owned Live state without reusing the Mock action state machine."""
        instance = self.get_instance(instance_id)
        if instance.mode.value != "live":
            return instance
        status_map = {
            "executing": InstanceStatus.RUNNING,
            "stopping": InstanceStatus.PAUSED,
            "completed": InstanceStatus.STOPPED,
            "stopped": InstanceStatus.STOPPED,
            "uncertain": InstanceStatus.WARNING,
        }
        projected = status_map.get(campaign_status)
        if projected is None:
            return instance
        phase_map = {
            "executing": "已绑定策略实盘执行中",
            "stopping": "已绑定策略安全停止中；等待撤单与成交核验",
            "completed": "已绑定策略本次授权完成；请核验成交账本",
            "stopped": "已绑定策略已安全停止",
            "uncertain": "已绑定策略结果待人工对账",
        }
        phase = phase_map[campaign_status]
        if reason:
            phase = f"{phase} ({reason[:64]})"
        if instance.status is projected and instance.phase == phase:
            return instance
        updated = instance.model_copy(
            update={
                "status": projected,
                "phase": phase,
                "cycle": instance.cycle.model_copy(update={"next_action_at": None}),
                "updated_at": "刚刚",
            },
            deep=True,
        )
        self.repository.replace(updated)
        return updated

    def pause_for_beta(self, instance_id: str, reason_code: str) -> AccountInstance:
        instance = self.get_instance(instance_id)
        if instance.status is not InstanceStatus.RUNNING:
            return instance
        system_pause_reason = f"beta:{reason_code}"[:96]
        progress = instance.strategy_progress.model_copy(update={"system_pause_reason": system_pause_reason})
        updated = instance.model_copy(
            update={
                "status": InstanceStatus.PAUSED,
                "phase": f"Beta 服务异常，系统已暂停 ({reason_code})",
                "cycle": instance.cycle.model_copy(update={"next_action_at": None}),
                "strategy_progress": progress,
                "updated_at": "刚刚",
                "unread_logs": instance.unread_logs + 1,
            },
            deep=True,
        )
        self.repository.replace(updated)
        self._append_log(
            instance_id,
            LogLevel.ERROR,
            f"Beta 服务异常：{reason_code}；策略已暂停，开始撤销活动挂单",
        )
        return updated

    def resume_beta_pause(self, instance_id: str) -> AccountInstance:
        instance = self.get_instance(instance_id)
        reason = instance.strategy_progress.system_pause_reason
        if instance.status is not InstanceStatus.PAUSED or reason is None or not reason.startswith("beta:"):
            return instance
        holding = instance.strategy_progress.stage is StrategyStage.HOLDING
        progress = instance.strategy_progress.model_copy(update={"system_pause_reason": None})
        updated = instance.model_copy(
            update={
                "status": InstanceStatus.RUNNING,
                "phase": "Beta 服务已恢复；继续等待平仓" if holding else "Beta 服务已恢复；策略继续运行",
                "cycle": instance.cycle.model_copy(update={"next_action_at": "等待平仓" if holding else "等待规划"}),
                "strategy_progress": progress,
                "updated_at": "刚刚",
                "unread_logs": instance.unread_logs + 1,
            },
            deep=True,
        )
        self.repository.replace(updated)
        self._append_log(instance_id, LogLevel.SUCCESS, "Beta 服务已恢复；系统暂停已自动解除")
        return updated

    def pause_for_position_mismatch(self, instance_id: str, reason_code: str) -> AccountInstance:
        instance = self.get_instance(instance_id)
        if instance.status is InstanceStatus.ERROR:
            return instance
        progress = instance.strategy_progress.model_copy(update={"system_pause_reason": f"position:{reason_code}"[:96]})
        updated = instance.model_copy(
            update={
                "status": InstanceStatus.PAUSED,
                "phase": f"仓位与周期不一致，系统已暂停 ({reason_code})",
                "cycle": instance.cycle.model_copy(update={"next_action_at": None}),
                "strategy_progress": progress,
                "updated_at": "刚刚",
                "unread_logs": instance.unread_logs + 1,
            },
            deep=True,
        )
        self.repository.replace(updated)
        self._append_log(
            instance_id,
            LogLevel.WARN,
            f"检测到仓位异常：{reason_code}；已暂停且不会自动补腿",
        )
        return updated

    def record_manual_pair_close(self, instance_id: str) -> AccountInstance:
        instance = self.get_instance(instance_id)
        if instance.strategy_progress.stage is StrategyStage.COMPLETE:
            phase = "已核对人工双腿平仓；目标交易量已完成"
        elif instance.status is InstanceStatus.RUNNING:
            phase = "已核对人工双腿平仓；等待下一轮"
        elif instance.status is InstanceStatus.PAUSED:
            phase = "已核对人工双腿平仓；实例仍保持暂停"
        else:
            phase = "已核对人工双腿平仓；实例保持停止"
        updated = instance.model_copy(
            update={
                "phase": phase,
                "updated_at": "刚刚",
                "unread_logs": instance.unread_logs + 1,
            },
            deep=True,
        )
        self.repository.replace(updated)
        self._append_log(
            instance_id,
            LogLevel.WARN,
            "检测到 BTC 与 ETH 已被人工全部平仓；已按真实成交历史继续核对进度",
        )
        return updated

    def record_positions_closed(
        self,
        instance_id: str,
        result: PositionCloseExecutionResult,
        aggregate: TradeVolumeAggregate,
        *,
        strategy_generated_volume_quote: Decimal | None,
    ) -> AccountInstance:
        if result.outcome.status is not CycleExecutionStatus.COMPLETED:
            raise ValueError("only a completed position close can update the account projection")
        instance = self.get_instance(instance_id)
        progress = instance.strategy_progress
        if (
            instance.strategy.target_mode is StrategyTargetMode.INCREMENTAL
            and strategy_generated_volume_quote is not None
        ):
            progress = progress.model_copy(
                update={"generated_volume_quote": max(Decimal(0), strategy_generated_volume_quote)}
            )

        record = result.record
        if record is not None:
            if record.status is not CycleExecutionStatus.COMPLETED or progress.active_cycle_id != record.plan.cycle_id:
                raise UnsafeOperation("completed close does not match the active pair cycle")
            completed_cycles = record.plan.sequence
        else:
            if progress.stage is StrategyStage.HOLDING or progress.active_cycle_id is not None:
                raise UnsafeOperation("snapshot close cannot replace an unmatched active pair cycle")
            completed_cycles = instance.cycle.completed

        volume = VolumeSnapshot(
            lifetime=float(aggregate.lifetime),
            today=float(aggregate.today),
            complete=aggregate.complete,
        )
        projected = instance.model_copy(
            update={"volume": volume, "strategy_progress": progress},
            deep=True,
        )
        target_reached = target_progress_quote(projected) >= instance.strategy.target_volume_quote
        progress = progress.model_copy(
            update={
                "stage": StrategyStage.COMPLETE
                if target_reached
                else (StrategyStage.COOLDOWN if record is not None else StrategyStage.IDLE),
                "next_action_at_ms": None,
                "active_cycle_id": None,
            }
        )
        if target_reached:
            phase = "一键平仓完成；目标交易量已完成"
        elif instance.status is InstanceStatus.PAUSED:
            phase = "一键平仓完成；策略保持暂停"
        elif instance.status in {InstanceStatus.WARNING, InstanceStatus.ERROR}:
            phase = "一键平仓完成；原状态保留待处理"
        else:
            phase = "一键平仓完成；策略保持停止"
        status = (
            InstanceStatus.STOPPED
            if target_reached and instance.status in {InstanceStatus.STOPPED, InstanceStatus.PAUSED}
            else instance.status
        )
        updated = instance.model_copy(
            update={
                "status": status,
                "phase": phase,
                "volume": volume,
                "exposure": ExposureSnapshot(),
                "cycle": instance.cycle.model_copy(update={"completed": completed_cycles, "next_action_at": None}),
                "strategy_progress": progress,
                "updated_at": "刚刚",
                "unread_logs": instance.unread_logs + 1,
            },
            deep=True,
        )
        updated = self._with_funding(updated)
        self.repository.replace(updated)
        amounts = {leg.symbol: leg.fill.quote_volume for leg in result.outcome.legs}
        self._append_log(
            instance_id,
            LogLevel.SUCCESS,
            f"一键平仓完成：BTC {amounts.get('BTCUSDT', Decimal(0))} + "
            f"ETH {amounts.get('ETHUSDT', Decimal(0))} USDT；"
            f"确认新增成交量 {result.closed_quote} USDT；策略未恢复",
        )
        return updated

    def record_order_cancel_verified(
        self,
        instance_id: str,
        *,
        canceled_count: int,
        reason: str,
        marks_stop_verified: bool = False,
    ) -> AccountInstance:
        instance = self.get_instance(instance_id)
        verified_at_ms = time.time_ns() // 1_000_000
        updated = instance.model_copy(
            update={
                "runtime": instance.runtime.model_copy(
                    update={"last_stop_verified_at_ms": verified_at_ms}
                    if marks_stop_verified
                    else {}
                ),
                "updated_at": "刚刚",
                "unread_logs": instance.unread_logs + 1,
            },
            deep=True,
        )
        self.repository.replace(updated)
        self._append_log(
            instance_id,
            LogLevel.SUCCESS,
            f"撤单核验完成：撤销 {canceled_count} 个活动挂单；结果 {reason}",
        )
        return updated

    def record_order_cancel_failure(self, instance_id: str, *, origin: str, reason: str) -> AccountInstance:
        instance = self.get_instance(instance_id)
        system_pause_reason = instance.strategy_progress.system_pause_reason or f"cancel_unverified:{origin}"
        progress = instance.strategy_progress.model_copy(update={"system_pause_reason": system_pause_reason[:96]})
        updated = instance.model_copy(
            update={
                "status": InstanceStatus.ERROR,
                "phase": f"撤单状态未核验，禁止继续运行 ({reason})",
                "cycle": instance.cycle.model_copy(update={"next_action_at": None}),
                "strategy_progress": progress,
                "runtime": instance.runtime.model_copy(
                    update={
                        "consecutive_failures": instance.runtime.consecutive_failures + 1,
                        "last_error_type": "OrderCancellationUnverified",
                        "last_stop_verified_at_ms": None,
                    }
                ),
                "updated_at": "刚刚",
                "unread_logs": instance.unread_logs + 1,
            },
            deep=True,
        )
        self.repository.replace(updated)
        self._append_log(
            instance_id,
            LogLevel.ERROR,
            f"撤单核验失败：{reason}；实例保持停止推进，必须重新核对",
        )
        return updated

    def record_global_stop(self, instance_id: str) -> AccountInstance:
        instance = self.get_instance(instance_id)
        updated = instance.model_copy(
            update={
                "phase": "全局停止已触发",
                "updated_at": "刚刚",
                "unread_logs": instance.unread_logs + 1,
            },
            deep=True,
        )
        self.repository.replace(updated)
        self._append_log(instance_id, LogLevel.WARN, "全局停止已完成，活动挂单撤销状态已核验")
        return updated
