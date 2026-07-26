from __future__ import annotations

from dataclasses import asdict

from weex_cli.execution.adaptive_maker import AdaptiveMakerPolicy, FixedBboPolicy

from .metrics import acceptance, aggregate, candidate_configs, hard_gates_pass, improvement, run_trials
from .model import BenchmarkConfig


def run_benchmark(config: BenchmarkConfig | None = None) -> dict[str, object]:
    benchmark = config or BenchmarkConfig()
    train_seeds = [benchmark.seed + index for index in range(benchmark.train_trials)]
    validation_seeds = [benchmark.seed + 100_000 + index for index in range(benchmark.validation_trials)]
    candidates = candidate_configs()
    training = [
        (
            candidate,
            aggregate(
                run_trials(lambda candidate=candidate: AdaptiveMakerPolicy(candidate), benchmark, train_seeds),
                benchmark,
            ),
        )
        for candidate in candidates
    ]
    eligible = [item for item in training if hard_gates_pass(item[1])] or training
    best_config, training_metrics = min(
        eligible,
        key=lambda item: (
            -item[1].success_rate,
            item[1].p95_elapsed_ms,
            item[1].mean_elapsed_ms,
            item[1].mean_requotes,
        ),
    )
    adaptive_results = run_trials(lambda: AdaptiveMakerPolicy(best_config), benchmark, validation_seeds)
    baseline_results = run_trials(lambda: FixedBboPolicy(5000), benchmark, validation_seeds)
    adaptive_metrics, baseline_metrics = aggregate(adaptive_results, benchmark), aggregate(baseline_results, benchmark)
    checks = acceptance(adaptive_metrics, baseline_metrics)
    return {
        "status": "passed" if all(checks.values()) else "failed",
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
            "mean_percent": improvement(baseline_metrics.mean_elapsed_ms, adaptive_metrics.mean_elapsed_ms),
            "p50_percent": improvement(baseline_metrics.p50_elapsed_ms, adaptive_metrics.p50_elapsed_ms),
            "p95_percent": improvement(baseline_metrics.p95_elapsed_ms, adaptive_metrics.p95_elapsed_ms),
        },
        "acceptance": checks,
        "validation_trials": [result.as_dict() for result in adaptive_results],
    }
