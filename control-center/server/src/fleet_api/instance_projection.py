from __future__ import annotations

from decimal import Decimal
from typing import Any, Protocol

from .models import AccountInstance, SessionVolumeProjection, StrategyStage, StrategyTargetMode
from .strategy_monitor import StrategyProgressProjection


class VolumeLedgerProjection(Protocol):
    def active_session(self, account_id: str, mode: str) -> dict[str, Any] | None: ...

    def latest_terminal_session(self, account_id: str, mode: str) -> dict[str, Any] | None: ...


class MonitorProjection(Protocol):
    def progress_for_session(self, instance_id: str, session_id: str | None) -> StrategyProgressProjection | None: ...


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
    elif active is not None:
        strategy_verified = Decimal(str(active["verified_quote_volume"]))
        strategy_remaining = Decimal(str(active["remaining_quote_volume"]))
        target_reached = False
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
    return instance.model_copy(
        update={
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
                }
            ),
            "strategy_progress": strategy_progress,
        }
    )


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
