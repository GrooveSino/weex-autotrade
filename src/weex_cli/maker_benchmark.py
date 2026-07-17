from __future__ import annotations

import math
import statistics
from collections.abc import Callable
from dataclasses import asdict, dataclass

from weex_cli.adaptive_executor import TargetRequest, execute_adaptive_maker_target
from weex_cli.adaptive_maker import AdaptiveMakerPolicy, FixedBboPolicy, MakerPolicy, MakerPolicyConfig
from weex_cli.errors import ValidationError
from weex_cli.maker_simulator import SimulatedMakerVenue, SimulationConfig


@dataclass(frozen=True)
class BenchmarkConfig:
    target_quote: float = 10_000.0
    cycles: int = 5
    train_trials: int = 15
    validation_trials: int = 15
    seed: int = 20_260_717
    per_leg_deadline_ms: int = 30_000
    poll_interval_ms: int = 100
    target_buffer: float = 1.02

    def __post_init__(self) -> None:
        if not math.isfinite(self.target_quote) or self.target_quote <= 0:
            raise ValidationError("benchmark target must be positive")
        if self.cycles < 5 or self.train_trials < 5 or self.validation_trials < 5:
            raise ValidationError("benchmark requires at least five cycles and five trials per phase")
        if self.per_leg_deadline_ms <= 0 or self.poll_interval_ms <= 0:
            raise ValidationError("benchmark timing must be positive")
        if not math.isfinite(self.target_buffer) or self.target_buffer < 1:
            raise ValidationError("benchmark target buffer must be at least one")


@dataclass(frozen=True)
class TrialResult:
    seed: int
    success: bool
    reason: str
    elapsed_ms: int
    quote_volume: float
    cycles_completed: int
    maker_fill_count: int
    maker_only: bool
    final_position: float
    max_overfill: float
    submissions: int
    cancels: int
    requotes: int
    post_only_rejections: int

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class AggregateMetrics:
    trials: int
    successes: int
    success_rate: float
    pure_maker_rate: float
    target_reached_rate: float
    flat_rate: float
    mean_elapsed_ms: float
    p50_elapsed_ms: float
    p95_elapsed_ms: float
    mean_cancels: float
    mean_requotes: float
    post_only_rejections: int
    minimum_volume: float
    minimum_maker_fills: int
    maximum_overfill: float

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def run_trial(
    policy_factory: Callable[[], MakerPolicy],
    config: BenchmarkConfig,
    seed: int,
    *,
    simulation_config: SimulationConfig | None = None,
) -> TrialResult:
    venue = SimulatedMakerVenue(seed, simulation_config)
    elapsed_ms = 0
    quote_volume = 0.0
    maker_fill_count = 0
    maker_only = True
    submissions = 0
    cancels = 0
    requotes = 0
    post_only_rejections = 0
    max_overfill = 0.0
    cycles_completed = 0
    reason = "completed"

    for cycle in range(config.cycles):
        remaining_cycles = config.cycles - cycle
        remaining_volume = max(0.0, config.target_quote - quote_volume)
        leg_notional = (
            max(
                config.target_quote / (config.cycles * 2),
                remaining_volume / (remaining_cycles * 2),
            )
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
            elapsed_ms += result.elapsed_ms
            quote_volume += result.quote_volume
            maker_fill_count += result.fill_count
            maker_only = maker_only and result.maker_only
            submissions += result.submissions
            cancels += result.cancels
            requotes += result.requotes
            post_only_rejections += result.post_only_rejections
            if side == "buy":
                max_overfill = max(max_overfill, max(0.0, result.final_position - target_position))
            else:
                max_overfill = max(max_overfill, max(0.0, -result.final_position))
            if result.status != "completed":
                reason = result.reason
                return _trial_result(
                    seed,
                    False,
                    reason,
                    elapsed_ms,
                    quote_volume,
                    cycles_completed,
                    maker_fill_count,
                    maker_only,
                    venue.position_quantity(),
                    max_overfill,
                    submissions,
                    cancels,
                    requotes,
                    post_only_rejections,
                )
        cycles_completed += 1

    final_position = venue.position_quantity()
    success = (
        cycles_completed >= config.cycles
        and maker_fill_count >= config.cycles * 2
        and maker_only
        and quote_volume >= config.target_quote
        and abs(final_position) <= 1e-9
        and max_overfill <= 1e-9
        and post_only_rejections == 0
    )
    if not success:
        reason = "acceptance_invariant_failed"
    return _trial_result(
        seed,
        success,
        reason,
        elapsed_ms,
        quote_volume,
        cycles_completed,
        maker_fill_count,
        maker_only,
        final_position,
        max_overfill,
        submissions,
        cancels,
        requotes,
        post_only_rejections,
    )


def run_benchmark(config: BenchmarkConfig | None = None) -> dict[str, object]:
    benchmark = config or BenchmarkConfig()
    train_seeds = [benchmark.seed + index for index in range(benchmark.train_trials)]
    validation_seeds = [benchmark.seed + 100_000 + index for index in range(benchmark.validation_trials)]

    candidates = _candidate_configs()
    training: list[tuple[MakerPolicyConfig, AggregateMetrics]] = []
    for candidate in candidates:
        results = _run_trials(lambda candidate=candidate: AdaptiveMakerPolicy(candidate), benchmark, train_seeds)
        training.append((candidate, aggregate(results, benchmark)))

    eligible = [item for item in training if _hard_gates_pass(item[1])]
    if not eligible:
        eligible = training
    best_config, training_metrics = min(
        eligible,
        key=lambda item: (
            -item[1].success_rate,
            item[1].p95_elapsed_ms,
            item[1].mean_elapsed_ms,
            item[1].mean_requotes,
        ),
    )

    adaptive_results = _run_trials(lambda: AdaptiveMakerPolicy(best_config), benchmark, validation_seeds)
    baseline_results = _run_trials(lambda: FixedBboPolicy(5000), benchmark, validation_seeds)
    adaptive_metrics = aggregate(adaptive_results, benchmark)
    baseline_metrics = aggregate(baseline_results, benchmark)
    acceptance = _acceptance(adaptive_metrics, baseline_metrics)

    return {
        "status": "passed" if all(acceptance.values()) else "failed",
        "simulation_only": True,
        "config": asdict(benchmark),
        "search": {
            "candidate_count": len(candidates),
            "train_seeds": train_seeds,
            "selected_policy": best_config.as_dict(),
            "selected_training_metrics": training_metrics.as_dict(),
        },
        "validation_seeds": validation_seeds,
        "adaptive": adaptive_metrics.as_dict(),
        "fixed_5000ms_baseline": baseline_metrics.as_dict(),
        "improvement": {
            "mean_percent": _improvement(baseline_metrics.mean_elapsed_ms, adaptive_metrics.mean_elapsed_ms),
            "p50_percent": _improvement(baseline_metrics.p50_elapsed_ms, adaptive_metrics.p50_elapsed_ms),
            "p95_percent": _improvement(baseline_metrics.p95_elapsed_ms, adaptive_metrics.p95_elapsed_ms),
        },
        "acceptance": acceptance,
        "validation_trials": [result.as_dict() for result in adaptive_results],
    }


def aggregate(results: list[TrialResult], config: BenchmarkConfig) -> AggregateMetrics:
    elapsed = [result.elapsed_ms for result in results]
    return AggregateMetrics(
        trials=len(results),
        successes=sum(result.success for result in results),
        success_rate=_rate(result.success for result in results),
        pure_maker_rate=_rate(result.maker_only and result.post_only_rejections == 0 for result in results),
        target_reached_rate=_rate(result.quote_volume >= config.target_quote for result in results),
        flat_rate=_rate(abs(result.final_position) <= 1e-9 for result in results),
        mean_elapsed_ms=statistics.fmean(elapsed),
        p50_elapsed_ms=statistics.median(elapsed),
        p95_elapsed_ms=_percentile(elapsed, 0.95),
        mean_cancels=statistics.fmean(result.cancels for result in results),
        mean_requotes=statistics.fmean(result.requotes for result in results),
        post_only_rejections=sum(result.post_only_rejections for result in results),
        minimum_volume=min(result.quote_volume for result in results),
        minimum_maker_fills=min(result.maker_fill_count for result in results),
        maximum_overfill=max(result.max_overfill for result in results),
    )


def _candidate_configs() -> list[MakerPolicyConfig]:
    candidates = []
    for min_rest_ms, max_rest_ms, stale_ticks in (
        (100, 600, 1),
        (100, 900, 1),
        (200, 900, 1),
        (200, 1200, 1),
        (200, 1500, 2),
        (300, 1800, 2),
    ):
        for min_probability, adverse_threshold in ((0.2, 0.7), (0.35, 0.55)):
            candidates.append(
                MakerPolicyConfig(
                    min_rest_ms=min_rest_ms,
                    max_rest_ms=max_rest_ms,
                    stale_ticks=stale_ticks,
                    min_fill_probability=min_probability,
                    adverse_threshold=adverse_threshold,
                    queue_ahead_factor=0.35,
                )
            )
    return candidates


def _run_trials(
    policy_factory: Callable[[], MakerPolicy],
    config: BenchmarkConfig,
    seeds: list[int],
) -> list[TrialResult]:
    return [run_trial(policy_factory, config, seed) for seed in seeds]


def _hard_gates_pass(metrics: AggregateMetrics) -> bool:
    return (
        metrics.success_rate == 1
        and metrics.pure_maker_rate == 1
        and metrics.target_reached_rate == 1
        and metrics.flat_rate == 1
        and metrics.post_only_rejections == 0
        and metrics.minimum_maker_fills >= 10
        and metrics.maximum_overfill <= 1e-9
    )


def _acceptance(adaptive: AggregateMetrics, baseline: AggregateMetrics) -> dict[str, bool]:
    return {
        "all_validation_trials_succeeded": adaptive.success_rate == 1,
        "all_fills_pure_maker": adaptive.pure_maker_rate == 1,
        "all_trials_reached_target": adaptive.target_reached_rate == 1,
        "all_trials_finished_flat": adaptive.flat_rate == 1,
        "at_least_ten_maker_fills": adaptive.minimum_maker_fills >= 10,
        "no_overfill": adaptive.maximum_overfill <= 1e-9,
        "no_post_only_rejections": adaptive.post_only_rejections == 0,
        "mean_faster_than_fixed": adaptive.mean_elapsed_ms < baseline.mean_elapsed_ms,
        "p50_faster_than_fixed": adaptive.p50_elapsed_ms < baseline.p50_elapsed_ms,
        "p95_faster_than_fixed": adaptive.p95_elapsed_ms < baseline.p95_elapsed_ms,
    }


def _trial_result(
    seed: int,
    success: bool,
    reason: str,
    elapsed_ms: int,
    quote_volume: float,
    cycles_completed: int,
    maker_fill_count: int,
    maker_only: bool,
    final_position: float,
    max_overfill: float,
    submissions: int,
    cancels: int,
    requotes: int,
    post_only_rejections: int,
) -> TrialResult:
    return TrialResult(
        seed=seed,
        success=success,
        reason=reason,
        elapsed_ms=elapsed_ms,
        quote_volume=quote_volume,
        cycles_completed=cycles_completed,
        maker_fill_count=maker_fill_count,
        maker_only=maker_only,
        final_position=final_position,
        max_overfill=max_overfill,
        submissions=submissions,
        cancels=cancels,
        requotes=requotes,
        post_only_rejections=post_only_rejections,
    )


def _rate(values) -> float:
    materialized = list(values)
    return sum(materialized) / len(materialized)


def _percentile(values: list[int], quantile: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    rank = (len(ordered) - 1) * quantile
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return float(ordered[lower])
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (rank - lower)


def _improvement(baseline: float, adaptive: float) -> float:
    if baseline <= 0:
        return 0.0
    return (baseline - adaptive) / baseline * 100
