from __future__ import annotations

import math
import random
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal, localcontext
from typing import TYPE_CHECKING

from .models import AccountInstance, StrategyTargetMode, VolumeStrategy

if TYPE_CHECKING:
    from .execution import PairAllocation


class StrategyTargetReached(RuntimeError):
    pass


class StrategyRunBlocked(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class StrategyRunPlan:
    target_mode: StrategyTargetMode
    run_disposition: str
    strategy_target_quote_volume: Decimal
    execution_target_quote_volume: Decimal
    baseline_lifetime_quote_volume: Decimal


def resolve_strategy_run_plan(
    instance: AccountInstance,
    active_session: dict[str, object] | None,
) -> StrategyRunPlan:
    if active_session is not None:
        status = str(active_session.get("status") or "")
        if status in {"active", "stopping", "recovering"}:
            raise StrategyRunBlocked(f"this account already has an operational strategy run ({status})")

    strategy_target = Decimal(instance.strategy.target_volume_quote)
    lifetime = Decimal(str(instance.volume.lifetime))
    if instance.strategy.target_mode is StrategyTargetMode.LIFETIME:
        if not instance.volume.complete:
            raise StrategyRunBlocked("complete lifetime trade history synchronization before starting")
        execution_target = max(strategy_target - lifetime, Decimal(0))
        if execution_target <= 0:
            raise StrategyTargetReached("the lifetime strategy target is already verified complete")
        return StrategyRunPlan(
            target_mode=StrategyTargetMode.LIFETIME,
            run_disposition="lifetime_residual",
            strategy_target_quote_volume=strategy_target,
            execution_target_quote_volume=execution_target,
            baseline_lifetime_quote_volume=lifetime,
        )

    return StrategyRunPlan(
        target_mode=StrategyTargetMode.INCREMENTAL,
        run_disposition="new_incremental",
        strategy_target_quote_volume=strategy_target,
        execution_target_quote_volume=strategy_target,
        baseline_lifetime_quote_volume=lifetime,
    )


@dataclass(frozen=True, slots=True)
class StrategyCycleSizing:
    btc_long_quote: Decimal
    eth_short_quote: Decimal
    total_open_quote: Decimal
    turnover_quote: Decimal
    sizing_mode: str


@dataclass(frozen=True, slots=True)
class RoundEstimate:
    minimum: int
    maximum: int


def plan_strategy_cycle(
    strategy: VolumeStrategy,
    target_progress_quote: Decimal,
    allocation: PairAllocation,
    rng: random.Random,
) -> StrategyCycleSizing:
    remaining = strategy.target_volume_quote - target_progress_quote
    if remaining <= 0:
        raise StrategyTargetReached("strategy target has already been reached")

    with localcontext() as context:
        context.prec = 50
        if remaining <= strategy.round_turnover_quote_max:
            turnover = remaining
            sizing_mode = "residual_finish"
        else:
            turnover = _random_quote(
                strategy.round_turnover_quote_min,
                strategy.round_turnover_quote_max,
                rng,
            )
            sizing_mode = "range_random"

        total_open = turnover / Decimal(2)
        btc_quote = total_open * allocation.btc_weight
        eth_quote = total_open * allocation.eth_weight

    return StrategyCycleSizing(
        btc_long_quote=btc_quote,
        eth_short_quote=eth_quote,
        total_open_quote=total_open,
        turnover_quote=turnover,
        sizing_mode=sizing_mode,
    )


def estimate_rounds(strategy: VolumeStrategy, target_progress_quote: Decimal | None = None) -> RoundEstimate:
    progress = target_progress_quote or Decimal(0)
    remaining = max(Decimal(0), strategy.target_volume_quote - progress)
    if remaining == 0:
        return RoundEstimate(0, 0)
    return RoundEstimate(
        minimum=_ceil_decimal(remaining / strategy.round_turnover_quote_max),
        maximum=_ceil_decimal(remaining / strategy.round_turnover_quote_min),
    )


def random_seconds(minimum: int, maximum: int, rng: random.Random) -> int:
    if minimum > maximum:
        raise ValueError("duration minimum cannot exceed maximum")
    return rng.randint(minimum, maximum)


def target_progress_quote(instance: AccountInstance, strategy: VolumeStrategy | None = None) -> Decimal:
    selected = strategy or instance.strategy
    if selected.target_mode is StrategyTargetMode.LIFETIME:
        return max(Decimal(0), Decimal(str(instance.volume.lifetime)))
    return instance.strategy_progress.generated_volume_quote


def target_tolerance_quote(target: Decimal) -> Decimal:
    proportional = target * Decimal("0.0025")
    if target >= Decimal("10000"):
        return min(Decimal("50"), proportional)
    return max(Decimal("1"), proportional)


def _random_quote(minimum: Decimal, maximum: Decimal, rng: random.Random) -> Decimal:
    minimum_cents = int((minimum * 100).to_integral_value(rounding=ROUND_CEILING))
    maximum_cents = int((maximum * 100).to_integral_value())
    if minimum_cents > maximum_cents:
        raise ValueError("round turnover range contains no cent-sized value")
    return Decimal(rng.randint(minimum_cents, maximum_cents)) / Decimal(100)


def _ceil_decimal(value: Decimal) -> int:
    return math.ceil(value)
