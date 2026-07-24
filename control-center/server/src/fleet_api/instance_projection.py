from __future__ import annotations

from decimal import Decimal
from typing import Any, Protocol

from .history_sync_projection import project_history_sync
from .models import AccountInstance, InstanceStatus, SessionVolumeProjection, StrategyStage, StrategyTargetMode
from .strategy_monitor import StrategyProgressProjection


class VolumeLedgerProjection(Protocol):
    def active_session(self, account_id: str, mode: str) -> dict[str, Any] | None: ...

    def latest_terminal_session(self, account_id: str, mode: str) -> dict[str, Any] | None: ...

    def sync_checkpoint(self, account_id: str, mode: str) -> dict[str, Any] | None: ...


class MonitorProjection(Protocol):
    def progress_for_session(self, instance_id: str, session_id: str | None) -> StrategyProgressProjection | None: ...


class LifecycleProjection(Protocol):
    def projection(self, instance_id: str, mode: str): ...


def optional_available_balance(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        balance = Decimal(str(value))
    except Exception:  # noqa: BLE001 - malformed audit metadata is absent, never substituted.
        return None
    return balance if balance.is_finite() and balance >= 0 else None


def project_instance_session(
    instance: AccountInstance,
    volume_ledger: VolumeLedgerProjection,
    strategy_monitor: MonitorProjection,
    lifecycle: LifecycleProjection | None = None,
) -> AccountInstance:
    active = volume_ledger.active_session(instance.id, instance.mode.value)
    last_run = volume_ledger.latest_terminal_session(instance.id, instance.mode.value)
    compatibility_session = active or last_run
    strategy_target = Decimal(instance.strategy.target_volume_quote)
    progress_source = "ledger"
    progress_updated_at_ms: int | None = None
    strategy_progress = instance.strategy_progress
    if instance.strategy.target_mode is StrategyTargetMode.LIFETIME:
        strategy_verified = Decimal(str(instance.volume.lifetime))
        strategy_remaining = max(strategy_target - strategy_verified, Decimal(0))
        target_reached = instance.volume.complete and strategy_remaining <= 0
    elif (current_run := active or _matching_terminal_run(instance, last_run)) is not None:
        strategy_verified = Decimal(str(current_run["verified_quote_volume"]))
        strategy_remaining = max(strategy_target - strategy_verified, Decimal(0))
        target_reached = str(current_run.get("status")) == "completed" and strategy_remaining <= 0
        progress_source = "ledger" if strategy_verified > 0 else "pending"
        if active is not None:
            strategy_verified, strategy_remaining, progress_source, progress_updated_at_ms, strategy_progress = (
                _apply_monitor_progress(
                    instance,
                    active,
                    strategy_monitor,
                    strategy_target,
                    strategy_verified,
                    strategy_remaining,
                    progress_source,
                    progress_updated_at_ms,
                    strategy_progress,
                )
            )
    else:
        strategy_verified = Decimal(0)
        strategy_remaining = strategy_target
        target_reached = False
        progress_source = "pending"
    lifecycle_projection = (
        lifecycle.projection(instance.id, instance.mode.value)
        if lifecycle is not None and instance.mode.value == "live"
        else instance.execution_lifecycle
    )
    projected_status, projected_phase = _lifecycle_account_state(instance, lifecycle_projection)
    checkpoint_reader = getattr(volume_ledger, "sync_checkpoint", None)
    checkpoint = checkpoint_reader(instance.id, instance.mode.value) if callable(checkpoint_reader) else None
    return instance.model_copy(
        update={
            "status": projected_status,
            "phase": projected_phase,
            "volume": instance.volume.model_copy(
                update={
                    "session": SessionVolumeProjection.model_validate(compatibility_session)
                    if compatibility_session is not None
                    else None,
                    "active_session": SessionVolumeProjection.model_validate(active) if active is not None else None,
                    "last_run": SessionVolumeProjection.model_validate(last_run) if last_run is not None else None,
                    "lifetime_source_complete": instance.volume.complete,
                    "strategy_target_quote_volume": strategy_target,
                    "strategy_verified_quote_volume": strategy_verified,
                    "strategy_remaining_quote_volume": strategy_remaining,
                    "strategy_target_reached": target_reached,
                    "strategy_progress_source": progress_source,
                    "strategy_progress_updated_at_ms": progress_updated_at_ms,
                    "history_sync": project_history_sync(checkpoint),
                }
            ),
            "strategy_progress": strategy_progress,
            "execution_lifecycle": lifecycle_projection,
        }
    )


def _matching_terminal_run(instance: AccountInstance, last_run: dict[str, Any] | None) -> dict[str, Any] | None:
    if last_run is None or last_run.get("strategy_id") != instance.strategy.id:
        return None
    try:
        version = int(last_run.get("strategy_version"))
    except (TypeError, ValueError):
        return None
    return last_run if version == instance.strategy.version else None


def _lifecycle_account_state(instance: AccountInstance, lifecycle: Any) -> tuple[InstanceStatus, str]:
    if instance.mode.value != "live":
        return instance.status, instance.phase
    states = {
        "idle": (InstanceStatus.STOPPED, "可启动已绑定策略"),
        "preparing": (InstanceStatus.STOPPED, "正在生成启动确认"),
        "running": (InstanceStatus.RUNNING, "已绑定策略执行中"),
        "stopping": (InstanceStatus.PAUSED, "安全停止中"),
        "recovering": (InstanceStatus.PAUSED, "后台只读核验中"),
        "recovery_cleanup_required": (InstanceStatus.WARNING, "当前任务仓位待安全收尾"),
        "orders_cleanup_required": (InstanceStatus.WARNING, "检测到启动前挂单，可先撤单"),
        "position_blocked": (InstanceStatus.WARNING, "账户已有仓位，关闭后可重新检查"),
    }
    return states.get(lifecycle.state, (InstanceStatus.STOPPED, "可启动已绑定策略"))


def _apply_monitor_progress(
    instance: AccountInstance,
    active: dict[str, Any],
    strategy_monitor: MonitorProjection,
    strategy_target: Decimal,
    strategy_verified: Decimal,
    strategy_remaining: Decimal,
    progress_source: str,
    progress_updated_at_ms: int | None,
    strategy_progress: Any,
) -> tuple[Decimal, Decimal, str, int | None, Any]:
    projection = strategy_monitor.progress_for_session(instance.id, str(active["session_id"]))
    if projection is None:
        return strategy_verified, strategy_remaining, progress_source, progress_updated_at_ms, strategy_progress
    if projection.verified_quote_volume > strategy_verified:
        strategy_verified = projection.verified_quote_volume
        strategy_remaining = max(strategy_target - strategy_verified, Decimal(0))
        progress_source = projection.volume_source
    progress_updated_at_ms = projection.updated_at_ms
    primary_wait = next((wait for wait in projection.active_waits if wait.key in {"hold", "round-gap"}), None)
    if primary_wait is not None:
        stage = StrategyStage.HOLDING if primary_wait.key == "hold" else StrategyStage.COOLDOWN
        strategy_progress = strategy_progress.model_copy(
            update={"stage": stage, "next_action_at_ms": primary_wait.deadline_at_ms}
        )
    if progress_source == "ledger" and strategy_verified <= 0:
        progress_source = "pending"
    return strategy_verified, strategy_remaining, progress_source, progress_updated_at_ms, strategy_progress
