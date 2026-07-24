from __future__ import annotations

import time
from decimal import Decimal

from .execution import CycleExecutionStatus, ExecutionRecord
from .models import (
    AccountInstance,
    ExposureSnapshot,
    InstanceStatus,
    LogLevel,
    ProxyStatus,
    StrategyStage,
    StrategyTargetMode,
    TradingMode,
)
from .service_errors import (
    ValidationFailed,
)
from .service_shared import delay_label as _delay_label
from .strategy import target_progress_quote
from .telemetry import AccountTelemetry


class ServiceTelemetryMixin:
    def apply_telemetry(
        self,
        instance_id: str,
        telemetry: AccountTelemetry,
        *,
        poll_started_at_ms: int | None = None,
        poll_completed_at_ms: int | None = None,
        poll_duration_ms: int | None = None,
        strategy_generated_volume_quote: Decimal | None = None,
    ) -> AccountInstance:
        instance = self.get_instance(instance_id)
        completed_at_ms = poll_completed_at_ms or time.time_ns() // 1_000_000
        recovered = instance.runtime.consecutive_failures > 0
        log_count = int(telemetry.activity_log is not None) + int(recovered)
        strategy_progress = instance.strategy_progress
        if (
            instance.strategy.target_mode is StrategyTargetMode.INCREMENTAL
            and strategy_generated_volume_quote is not None
        ):
            strategy_progress = strategy_progress.model_copy(
                update={"generated_volume_quote": max(Decimal(0), strategy_generated_volume_quote)}
            )
        updated = instance.model_copy(
            update={
                # Telemetry owns account observations only. Execution status and
                # phase are projected from the strategy lifecycle elsewhere.
                "phase": instance.phase,
                "wallet": telemetry.wallet,
                "volume": telemetry.volume,
                "exposure": telemetry.exposure,
                "proxy": instance.proxy.model_copy(
                    update={
                        "latency_ms": telemetry.proxy_latency_ms,
                        "status": telemetry.proxy_status,
                        "location": telemetry.proxy_location,
                    }
                ),
                "cycle": instance.cycle.model_copy(update={"completed": telemetry.cycle_completed}),
                "strategy_progress": strategy_progress,
                "runtime": instance.runtime.model_copy(
                    update={
                        "last_poll_started_at_ms": poll_started_at_ms or completed_at_ms,
                        "last_poll_succeeded_at_ms": completed_at_ms,
                        "last_poll_duration_ms": poll_duration_ms,
                        "consecutive_failures": 0,
                        "last_error_type": None,
                    }
                ),
                "updated_at": "刚刚",
                "unread_logs": instance.unread_logs + log_count,
            },
            deep=True,
        )
        updated = self._with_funding(updated, wallet_known=True)
        self.repository.replace(updated)
        if recovered:
            self._append_log(instance_id, LogLevel.SUCCESS, "遥测连接已恢复")
        if telemetry.activity_log is not None:
            self._append_log(instance_id, LogLevel.INFO, telemetry.activity_log)
        return updated

    def record_strategy_execution(
        self,
        instance_id: str,
        record: ExecutionRecord,
        *,
        submitted: bool,
    ) -> AccountInstance:
        instance = self.get_instance(instance_id)
        plan = record.plan
        if record.status is CycleExecutionStatus.OPENED:
            if not submitted or instance.strategy_progress.active_cycle_id == plan.cycle_id:
                return instance
            next_action_at_ms = record.updated_at_ms + plan.position_hold_seconds * 1_000
            ratio = plan.eth_short_quote / plan.btc_long_quote
            progress = instance.strategy_progress.model_copy(
                update={
                    "stage": StrategyStage.HOLDING,
                    "next_action_at_ms": next_action_at_ms,
                    "active_cycle_id": plan.cycle_id,
                    "last_eth_ratio": ratio,
                    "last_allocation_version": plan.allocation_version,
                }
            )
            updated = instance.model_copy(
                update={
                    "phase": "BTC 多 / ETH 空已开仓，等待平仓",
                    "exposure": ExposureSnapshot(
                        btc_long=float(plan.btc_long_quote),
                        eth_short=float(plan.eth_short_quote),
                    ),
                    "cycle": instance.cycle.model_copy(
                        update={"next_action_at": _delay_label(plan.position_hold_seconds, "平仓")}
                    ),
                    "strategy_progress": progress,
                    "updated_at": "刚刚",
                    "unread_logs": instance.unread_logs + 1,
                },
                deep=True,
            )
            self.repository.replace(updated)
            residual = "；本轮按目标残差收尾" if plan.sizing_mode == "residual_finish" else ""
            self._append_log(
                instance_id,
                LogLevel.SUCCESS,
                f"周期 {plan.sequence} 已开仓：BTC {plan.btc_long_quote} + ETH {plan.eth_short_quote} USDT；"
                f"{plan.position_hold_seconds} 秒后平仓{residual}",
            )
            return updated

        if record.status is not CycleExecutionStatus.COMPLETED:
            return instance
        if not submitted or instance.strategy_progress.active_cycle_id != plan.cycle_id:
            return instance
        generated = instance.strategy_progress.generated_volume_quote
        if (
            instance.strategy.target_mode is StrategyTargetMode.INCREMENTAL
            and instance.strategy_progress.started_at_ms is None
        ):
            generated += plan.turnover_quote or plan.total_quote * 2
        progress_before_state = instance.strategy_progress.model_copy(update={"generated_volume_quote": generated})
        projected = instance.model_copy(update={"strategy_progress": progress_before_state}, deep=True)
        achieved = target_progress_quote(projected)
        target_reached = achieved >= instance.strategy.target_volume_quote
        next_action_at_ms = None if target_reached else record.updated_at_ms + plan.round_interval_seconds * 1_000
        progress = progress_before_state.model_copy(
            update={
                "generated_volume_quote": generated,
                "stage": StrategyStage.COMPLETE if target_reached else StrategyStage.COOLDOWN,
                "next_action_at_ms": next_action_at_ms,
                "active_cycle_id": None,
            }
        )
        updated = instance.model_copy(
            update={
                "status": InstanceStatus.STOPPED if target_reached else instance.status,
                "phase": "目标交易量已完成" if target_reached else "本轮已平仓，等待下一轮",
                "exposure": ExposureSnapshot(),
                "cycle": instance.cycle.model_copy(
                    update={
                        "completed": plan.sequence,
                        "next_action_at": (
                            None if target_reached else _delay_label(plan.round_interval_seconds, "下一轮")
                        ),
                    }
                ),
                "strategy_progress": progress,
                "updated_at": "刚刚",
                "unread_logs": instance.unread_logs + 1,
            },
            deep=True,
        )
        self.repository.replace(updated)
        self._append_log(
            instance_id,
            LogLevel.SUCCESS,
            f"周期 {plan.sequence} 已平仓；本轮贡献 {plan.turnover_quote} USDT，"
            f"策略进度 {achieved}/{instance.strategy.target_volume_quote} USDT",
        )
        if target_reached:
            self._append_log(instance_id, LogLevel.SUCCESS, "已精确达到目标交易量；运行已停止")
        return updated

    def set_volume_completeness(self, instance_id: str, complete: bool) -> AccountInstance:
        instance = self.get_instance(instance_id)
        updated = instance.model_copy(
            update={"volume": instance.volume.model_copy(update={"complete": complete})},
            deep=True,
        )
        self.repository.replace(updated)
        return updated

    def record_runtime_failure(
        self,
        instance_id: str,
        failure_type: str,
        *,
        poll_started_at_ms: int | None = None,
        poll_failed_at_ms: int | None = None,
        poll_duration_ms: int | None = None,
    ) -> AccountInstance:
        instance = self.get_instance(instance_id)
        failed_at_ms = poll_failed_at_ms or time.time_ns() // 1_000_000
        legacy_telemetry_error = (
            instance.status is InstanceStatus.ERROR
            and instance.strategy_progress.system_pause_reason is None
            and instance.phase.startswith("数据同步失败 (")
        )
        if instance.status is InstanceStatus.STOPPED or legacy_telemetry_error:
            status = InstanceStatus.STOPPED
            phase = f"已停止；数据待核验 ({failure_type})"
        elif instance.status is InstanceStatus.PAUSED:
            status = InstanceStatus.PAUSED
            phase = f"已暂停；数据待核验 ({failure_type})"
        elif instance.status in {InstanceStatus.RUNNING, InstanceStatus.WARNING}:
            status = InstanceStatus.WARNING
            phase = f"运行已安全暂停；数据待核验 ({failure_type})"
        else:
            # Protected execution/cancellation errors must never be downgraded
            # by a later read-only telemetry failure.
            status = InstanceStatus.ERROR
            phase = instance.phase
        should_log = (
            instance.status is not status or instance.phase != phase or instance.runtime.last_error_type != failure_type
        )
        updated = instance.model_copy(
            update={
                "status": status,
                "phase": phase,
                "proxy": instance.proxy.model_copy(update={"status": ProxyStatus.DEGRADED}),
                "cycle": instance.cycle.model_copy(update={"next_action_at": None}),
                "runtime": instance.runtime.model_copy(
                    update={
                        "last_poll_started_at_ms": poll_started_at_ms or failed_at_ms,
                        "last_poll_failed_at_ms": failed_at_ms,
                        "last_poll_duration_ms": poll_duration_ms,
                        "consecutive_failures": instance.runtime.consecutive_failures + 1,
                        "last_error_type": failure_type,
                    }
                ),
                "updated_at": "刚刚",
                "unread_logs": instance.unread_logs + int(should_log),
            },
            deep=True,
        )
        self.repository.replace(updated)
        if should_log:
            self._append_log(instance_id, LogLevel.ERROR, f"遥测失败：{failure_type}")
        return updated

    def record_execution_failure(self, instance_id: str, status: str, reason: str) -> AccountInstance:
        instance = self.get_instance(instance_id)
        phase = f"配对执行{status}: {reason}"
        updated = instance.model_copy(
            update={
                "status": InstanceStatus.ERROR,
                "phase": phase,
                "cycle": instance.cycle.model_copy(update={"next_action_at": None}),
                "updated_at": "刚刚",
                "unread_logs": instance.unread_logs + 1,
            },
            deep=True,
        )
        self.repository.replace(updated)
        self._append_log(instance_id, LogLevel.ERROR, f"配对执行 {status}：{reason}")
        return updated

    def stop_at_cycle_target(self, instance_id: str) -> AccountInstance:
        instance = self.get_instance(instance_id)
        if target_progress_quote(instance) < instance.strategy.target_volume_quote:
            raise ValidationFailed("volume target has not been reached")
        if instance.status is InstanceStatus.STOPPED and instance.phase == "目标交易量已完成":
            return instance
        updated = instance.model_copy(
            update={
                "status": InstanceStatus.STOPPED,
                "phase": "目标交易量已完成",
                "cycle": instance.cycle.model_copy(update={"next_action_at": None}),
                "updated_at": "刚刚",
                "unread_logs": instance.unread_logs + 1,
            },
            deep=True,
        )
        self.repository.replace(updated)
        self._append_log(instance_id, LogLevel.SUCCESS, "已达到目标交易量；运行已停止")
        return updated

    def reconcile_after_restart(self) -> int:
        reconciled = 0
        for instance in self.repository.list():
            # Live execution state belongs exclusively to StrategyRunLifecycleService.
            # Telemetry restart recovery may refresh observations, but must never
            # stop, resume, or otherwise rewrite a live strategy lifecycle.
            if instance.mode is TradingMode.LIVE:
                continue
            if instance.status in {InstanceStatus.STOPPED, InstanceStatus.ERROR}:
                continue
            reconciled += 1
            updated = instance.model_copy(
                update={
                    "status": InstanceStatus.STOPPED,
                    "phase": "服务已重启，正在恢复运行状态",
                    "cycle": instance.cycle.model_copy(update={"next_action_at": None}),
                    "updated_at": "刚刚",
                    "unread_logs": instance.unread_logs + 1,
                },
                deep=True,
            )
            self.repository.replace(updated)
            self._append_log(instance.id, LogLevel.WARN, "检测到服务重启；正在只读恢复运行状态")
        return reconciled
