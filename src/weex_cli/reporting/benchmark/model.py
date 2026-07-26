from __future__ import annotations

import math
from dataclasses import asdict, dataclass

from weex_cli.core.errors import ValidationError


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
