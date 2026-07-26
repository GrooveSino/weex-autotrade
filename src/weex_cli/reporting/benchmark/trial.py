from __future__ import annotations

from collections.abc import Callable

from weex_cli.execution.adaptive import TargetRequest, execute_adaptive_maker_target
from weex_cli.execution.adaptive_maker import MakerPolicy
from weex_cli.execution.maker_simulator import SimulatedMakerVenue, SimulationConfig

from .model import BenchmarkConfig, TrialResult


def run_trial(
    policy_factory: Callable[[], MakerPolicy],
    config: BenchmarkConfig,
    seed: int,
    *,
    simulation_config: SimulationConfig | None = None,
) -> TrialResult:
    venue = SimulatedMakerVenue(seed, simulation_config)
    totals = {
        "elapsed_ms": 0,
        "quote_volume": 0.0,
        "maker_fill_count": 0,
        "submissions": 0,
        "cancels": 0,
        "requotes": 0,
        "post_only_rejections": 0,
    }
    maker_only, max_overfill, cycles_completed, reason = True, 0.0, 0, "completed"
    for cycle in range(config.cycles):
        remaining_cycles = config.cycles - cycle
        remaining_volume = max(0.0, config.target_quote - totals["quote_volume"])
        leg_notional = (
            max(config.target_quote / (config.cycles * 2), remaining_volume / (remaining_cycles * 2))
            * config.target_buffer
        )
        quantity = leg_notional / venue.snapshot().mid
        for side, target_position in (("buy", quantity), ("sell", 0.0)):
            result = execute_adaptive_maker_target(
                venue,
                policy_factory(),
                TargetRequest(
                    side=side,
                    target_position=target_position,
                    deadline_ms=config.per_leg_deadline_ms,
                    poll_interval_ms=config.poll_interval_ms,
                    max_requotes=100,
                    tolerance_quantity=1e-10,
                    client_prefix=f"bench-{seed}-{cycle}-{side}",
                ),
            )
            totals["elapsed_ms"] += result.elapsed_ms
            totals["quote_volume"] += result.quote_volume
            totals["maker_fill_count"] += result.fill_count
            totals["submissions"] += result.submissions
            totals["cancels"] += result.cancels
            totals["requotes"] += result.requotes
            totals["post_only_rejections"] += result.post_only_rejections
            maker_only = maker_only and result.maker_only
            max_overfill = max(
                max_overfill,
                max(0.0, result.final_position - target_position)
                if side == "buy"
                else max(0.0, -result.final_position),
            )
            if result.status != "completed":
                reason = result.reason
                return _trial_result(
                    seed, False, reason, totals, cycles_completed, maker_only, venue.position_quantity(), max_overfill
                )
        cycles_completed += 1
    final_position = venue.position_quantity()
    success = (
        cycles_completed >= config.cycles
        and totals["maker_fill_count"] >= config.cycles * 2
        and maker_only
        and totals["quote_volume"] >= config.target_quote
        and abs(final_position) <= 1e-9
        and max_overfill <= 1e-9
        and totals["post_only_rejections"] == 0
    )
    return _trial_result(
        seed,
        success,
        reason if success else "acceptance_invariant_failed",
        totals,
        cycles_completed,
        maker_only,
        final_position,
        max_overfill,
    )


def _trial_result(
    seed: int,
    success: bool,
    reason: str,
    totals: dict[str, float | int],
    cycles_completed: int,
    maker_only: bool,
    final_position: float,
    max_overfill: float,
) -> TrialResult:
    return TrialResult(
        seed=seed,
        success=success,
        reason=reason,
        cycles_completed=cycles_completed,
        maker_only=maker_only,
        final_position=final_position,
        max_overfill=max_overfill,
        **totals,
    )
