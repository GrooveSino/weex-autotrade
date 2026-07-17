from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Literal, Protocol

from weex_cli.errors import ValidationError

Side = Literal["buy", "sell"]
DecisionAction = Literal["quote", "hold", "cancel"]


@dataclass(frozen=True)
class MarketSnapshot:
    timestamp_ms: int
    bid: float
    ask: float
    bid_size: float
    ask_size: float
    buy_flow_per_sec: float
    sell_flow_per_sec: float
    volatility_ticks: float
    tick_size: float

    def __post_init__(self) -> None:
        values = (
            self.bid,
            self.ask,
            self.bid_size,
            self.ask_size,
            self.buy_flow_per_sec,
            self.sell_flow_per_sec,
            self.volatility_ticks,
            self.tick_size,
        )
        if any(not math.isfinite(value) or value < 0 for value in values):
            raise ValidationError("market snapshot values must be finite and nonnegative")
        if self.bid <= 0 or self.ask <= self.bid or self.tick_size <= 0:
            raise ValidationError("market snapshot must have positive bid, ask, tick size, and spread")

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def spread_ticks(self) -> int:
        return max(1, round((self.ask - self.bid) / self.tick_size))

    @property
    def book_imbalance(self) -> float:
        total = self.bid_size + self.ask_size
        return 0.0 if total <= 0 else (self.bid_size - self.ask_size) / total

    @property
    def flow_imbalance(self) -> float:
        total = self.buy_flow_per_sec + self.sell_flow_per_sec
        return 0.0 if total <= 0 else (self.buy_flow_per_sec - self.sell_flow_per_sec) / total

    @property
    def microprice(self) -> float:
        total = self.bid_size + self.ask_size
        if total <= 0:
            return self.mid
        return (self.ask * self.bid_size + self.bid * self.ask_size) / total


@dataclass(frozen=True)
class MakerPolicyConfig:
    min_rest_ms: int = 250
    max_rest_ms: int = 1500
    stale_ticks: int = 1
    improve_spread_ticks: int = 2
    min_fill_probability: float = 0.35
    adverse_threshold: float = 0.55
    queue_ahead_factor: float = 0.65
    urgency_weight: float = 0.8
    child_fraction: float = 1.0
    passive_guard_ticks: int = 0
    urgent_guard_ticks: int = 0
    max_passive_guard_ticks: int = 0
    volatility_guard_multiplier: float = 0.0

    def __post_init__(self) -> None:
        if self.min_rest_ms < 0 or self.max_rest_ms <= self.min_rest_ms:
            raise ValidationError("max_rest_ms must be greater than min_rest_ms")
        if self.stale_ticks < 1 or self.improve_spread_ticks < 2:
            raise ValidationError("stale_ticks and improve_spread_ticks are invalid")
        for name in ("min_fill_probability", "adverse_threshold", "queue_ahead_factor", "child_fraction"):
            value = float(getattr(self, name))
            if not 0 < value <= 1:
                raise ValidationError(f"{name} must be in (0, 1]")
        if self.urgency_weight < 0:
            raise ValidationError("urgency_weight must be nonnegative")
        if self.passive_guard_ticks < 0:
            raise ValidationError("passive_guard_ticks must be nonnegative")
        if not 0 <= self.urgent_guard_ticks <= self.passive_guard_ticks:
            raise ValidationError("urgent_guard_ticks must be between zero and passive_guard_ticks")
        if self.max_passive_guard_ticks < 0:
            raise ValidationError("max_passive_guard_ticks must be nonnegative")
        if self.volatility_guard_multiplier < 0:
            raise ValidationError("volatility_guard_multiplier must be nonnegative")

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class WorkingQuote:
    side: Side
    price: float
    submitted_ms: int
    queue_ahead: float
    remaining_quantity: float


@dataclass(frozen=True)
class QuoteDecision:
    action: DecisionAction
    price: float | None
    fill_probability: float
    expected_fill_ms: float
    adverse_score: float
    reason: str


class MakerPolicy(Protocol):
    config: MakerPolicyConfig

    def decide(
        self,
        snapshot: MarketSnapshot,
        side: Side,
        remaining_quantity: float,
        urgency: float,
        working: WorkingQuote | None = None,
    ) -> QuoteDecision: ...


class AdaptiveMakerPolicy:
    def __init__(self, config: MakerPolicyConfig) -> None:
        self.config = config

    def decide(
        self,
        snapshot: MarketSnapshot,
        side: Side,
        remaining_quantity: float,
        urgency: float,
        working: WorkingQuote | None = None,
    ) -> QuoteDecision:
        if remaining_quantity <= 0:
            raise ValidationError("remaining_quantity must be positive")
        urgency = min(1.0, max(0.0, urgency))
        adverse = self._adverse_score(snapshot, side)
        if working is None:
            return self._new_quote(snapshot, side, remaining_quantity, urgency, adverse)

        age = max(0, snapshot.timestamp_ms - working.submitted_ms)
        probability, expected = self._fill_estimate(snapshot, side, working.remaining_quantity, working.queue_ahead)
        stale = self._stale_ticks(snapshot, working)
        if age < self.config.min_rest_ms:
            return QuoteDecision("hold", working.price, probability, expected, adverse, "minimum_residence")
        if stale >= self.config.stale_ticks:
            return QuoteDecision("cancel", None, probability, expected, adverse, "stale_price")
        if age >= self.config.max_rest_ms:
            return QuoteDecision("cancel", None, probability, expected, adverse, "maximum_residence")
        if adverse >= self.config.adverse_threshold and urgency < 0.85:
            return QuoteDecision("cancel", None, probability, expected, adverse, "adverse_selection")
        if probability < self.config.min_fill_probability and expected > self.config.max_rest_ms:
            return QuoteDecision("cancel", None, probability, expected, adverse, "low_fill_probability")
        return QuoteDecision("hold", working.price, probability, expected, adverse, "queue_is_competitive")

    def _new_quote(
        self,
        snapshot: MarketSnapshot,
        side: Side,
        quantity: float,
        urgency: float,
        adverse: float,
    ) -> QuoteDecision:
        guard_floor = math.ceil(
            self.config.passive_guard_ticks
            - (self.config.passive_guard_ticks - self.config.urgent_guard_ticks) * urgency
        )
        volatility_scale = 1 - 0.75 * urgency
        dynamic_guard = math.ceil(
            snapshot.volatility_ticks * self.config.volatility_guard_multiplier * volatility_scale
        )
        guard_ticks = min(
            max(self.config.passive_guard_ticks, self.config.max_passive_guard_ticks),
            max(guard_floor, dynamic_guard),
        )
        guard = guard_ticks * snapshot.tick_size
        candidates = [snapshot.bid - guard if side == "buy" else snapshot.ask + guard]
        if self.config.passive_guard_ticks == 0 and snapshot.spread_ticks >= self.config.improve_spread_ticks:
            improved = candidates[0] + snapshot.tick_size if side == "buy" else candidates[0] - snapshot.tick_size
            if snapshot.bid < improved < snapshot.ask:
                candidates.append(improved)

        best: QuoteDecision | None = None
        best_score = -math.inf
        for price in candidates:
            at_book = math.isclose(price, snapshot.bid if side == "buy" else snapshot.ask)
            displayed = snapshot.bid_size if side == "buy" else snapshot.ask_size
            queue = displayed * self.config.queue_ahead_factor if at_book else 0.0
            probability, expected = self._fill_estimate(snapshot, side, quantity, queue)
            improvement_penalty = 0.08 if not at_book else 0.0
            score = probability * (1 + urgency * self.config.urgency_weight) - adverse - improvement_penalty
            decision = QuoteDecision(
                "quote",
                price,
                probability,
                expected,
                adverse,
                f"score={score:.6f};guard_ticks={guard_ticks}",
            )
            if score > best_score:
                best = decision
                best_score = score
        assert best is not None
        return best

    def _fill_estimate(
        self, snapshot: MarketSnapshot, side: Side, quantity: float, queue_ahead: float
    ) -> tuple[float, float]:
        opposing_flow = snapshot.sell_flow_per_sec if side == "buy" else snapshot.buy_flow_per_sec
        effective_flow = max(1e-9, opposing_flow)
        expected_ms = max(25.0, (queue_ahead + quantity * 0.5) / effective_flow * 1000)
        probability = 1 - math.exp(-self.config.max_rest_ms / expected_ms)
        return min(1.0, max(0.0, probability)), expected_ms

    @staticmethod
    def _adverse_score(snapshot: MarketSnapshot, side: Side) -> float:
        micro_edge = (snapshot.microprice - snapshot.mid) / snapshot.tick_size
        directional = 0.5 * micro_edge + 0.3 * snapshot.book_imbalance + 0.2 * snapshot.flow_imbalance
        if side == "sell":
            directional = -directional
        volatility_penalty = min(0.3, max(0.0, snapshot.volatility_ticks - 1) * 0.05)
        return min(1.0, max(0.0, -directional + volatility_penalty))

    @staticmethod
    def _stale_ticks(snapshot: MarketSnapshot, working: WorkingQuote) -> float:
        if working.side == "buy":
            return max(0.0, (snapshot.bid - working.price) / snapshot.tick_size)
        return max(0.0, (working.price - snapshot.ask) / snapshot.tick_size)


class FixedBboPolicy:
    def __init__(self, rest_ms: int = 5000) -> None:
        self.config = MakerPolicyConfig(
            min_rest_ms=max(0, rest_ms - 1),
            max_rest_ms=rest_ms,
            stale_ticks=10_000,
            min_fill_probability=0.01,
            adverse_threshold=1.0,
        )

    def decide(
        self,
        snapshot: MarketSnapshot,
        side: Side,
        remaining_quantity: float,
        urgency: float,
        working: WorkingQuote | None = None,
    ) -> QuoteDecision:
        if working is None:
            price = snapshot.bid if side == "buy" else snapshot.ask
            return QuoteDecision("quote", price, 0.0, float(self.config.max_rest_ms), 0.0, "fixed_bbo")
        age = snapshot.timestamp_ms - working.submitted_ms
        action: DecisionAction = "cancel" if age >= self.config.max_rest_ms else "hold"
        return QuoteDecision(action, working.price if action == "hold" else None, 0.0, float(age), 0.0, "fixed_timer")
