from __future__ import annotations

import math
import statistics
from collections.abc import Callable, Iterable

from weex_cli.execution.adaptive_maker import MakerPolicy, MakerPolicyConfig

from .model import AggregateMetrics, BenchmarkConfig, TrialResult
from .trial import run_trial


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


def candidate_configs() -> list[MakerPolicyConfig]:
    return [
        MakerPolicyConfig(
            min_rest_ms=min_rest_ms,
            max_rest_ms=max_rest_ms,
            stale_ticks=stale_ticks,
            min_fill_probability=min_probability,
            adverse_threshold=adverse_threshold,
            queue_ahead_factor=0.35,
        )
        for min_rest_ms, max_rest_ms, stale_ticks in (
            (100, 600, 1),
            (100, 900, 1),
            (200, 900, 1),
            (200, 1200, 1),
            (200, 1500, 2),
            (300, 1800, 2),
        )
        for min_probability, adverse_threshold in ((0.2, 0.7), (0.35, 0.55))
    ]


def run_trials(
    policy_factory: Callable[[], MakerPolicy], config: BenchmarkConfig, seeds: list[int]
) -> list[TrialResult]:
    return [run_trial(policy_factory, config, seed) for seed in seeds]


def hard_gates_pass(metrics: AggregateMetrics) -> bool:
    return (
        metrics.success_rate == 1
        and metrics.pure_maker_rate == 1
        and metrics.target_reached_rate == 1
        and metrics.flat_rate == 1
        and metrics.post_only_rejections == 0
        and metrics.minimum_maker_fills >= 10
        and metrics.maximum_overfill <= 1e-9
    )


def acceptance(adaptive: AggregateMetrics, baseline: AggregateMetrics) -> dict[str, bool]:
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


def improvement(baseline: float, adaptive: float) -> float:
    return 0.0 if baseline <= 0 else (baseline - adaptive) / baseline * 100


def _rate(values: Iterable[bool]) -> float:
    materialized = list(values)
    return sum(materialized) / len(materialized)


def _percentile(values: list[int], quantile: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    rank = (len(ordered) - 1) * quantile
    lower, upper = math.floor(rank), math.ceil(rank)
    if lower == upper:
        return float(ordered[lower])
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (rank - lower)
