from __future__ import annotations

from dataclasses import dataclass, replace

import pytest

from weex_cli.core.errors import ValidationError
from weex_cli.execution.adaptive import TargetRequest, VenueOrder, execute_adaptive_maker_target
from weex_cli.execution.adaptive_maker import (
    AdaptiveMakerPolicy,
    MakerPolicyConfig,
    MarketSnapshot,
    QuoteDecision,
    Side,
    WorkingQuote,
)
from weex_cli.execution.maker_simulator import SimulatedMakerVenue, SimulationConfig
from weex_cli.reporting.benchmark import BenchmarkConfig, run_benchmark, run_trial


def snapshot(*, timestamp_ms: int = 1000, bid: float = 100, ask: float = 100.2) -> MarketSnapshot:
    return MarketSnapshot(
        timestamp_ms=timestamp_ms,
        bid=bid,
        ask=ask,
        bid_size=0.02,
        ask_size=0.02,
        buy_flow_per_sec=0.04,
        sell_flow_per_sec=0.04,
        volatility_ticks=1.0,
        tick_size=0.1,
    )


def test_policy_quotes_never_cross_the_book() -> None:
    policy = AdaptiveMakerPolicy(MakerPolicyConfig())
    market = snapshot()

    buy = policy.decide(market, "buy", 0.01, 0)
    sell = policy.decide(market, "sell", 0.01, 0)

    assert buy.action == "quote" and buy.price is not None and buy.price < market.ask
    assert sell.action == "quote" and sell.price is not None and sell.price > market.bid


def test_same_side_policy_quotes_exact_bid1_and_ask1_even_with_a_wide_spread() -> None:
    policy = AdaptiveMakerPolicy(MakerPolicyConfig(allow_inside_spread=False))
    market = snapshot(bid=100, ask=101)

    buy = policy.decide(market, "buy", 0.01, 0)
    sell = policy.decide(market, "sell", 0.01, 0)

    assert buy.price == pytest.approx(market.bid)
    assert sell.price == pytest.approx(market.ask)


def test_passive_guard_quotes_one_tick_behind_bbo() -> None:
    policy = AdaptiveMakerPolicy(MakerPolicyConfig(passive_guard_ticks=1))
    market = snapshot()

    buy = policy.decide(market, "buy", 0.01, 0)
    sell = policy.decide(market, "sell", 0.01, 0)

    assert buy.price == pytest.approx(market.bid - market.tick_size)
    assert sell.price == pytest.approx(market.ask + market.tick_size)


def test_dynamic_guard_scales_with_volatility_and_respects_cap() -> None:
    policy = AdaptiveMakerPolicy(
        MakerPolicyConfig(
            passive_guard_ticks=5,
            max_passive_guard_ticks=20,
            volatility_guard_multiplier=2,
        )
    )
    market = replace(snapshot(), volatility_ticks=8.2)

    buy = policy.decide(market, "buy", 0.01, 0)
    sell = policy.decide(market, "sell", 0.01, 0)

    assert buy.price == pytest.approx(market.bid - 17 * market.tick_size)
    assert sell.price == pytest.approx(market.ask + 17 * market.tick_size)
    capped = policy.decide(replace(market, volatility_ticks=100), "sell", 0.01, 0)
    assert capped.price == pytest.approx(market.ask + 20 * market.tick_size)


def test_guard_tapers_toward_bbo_as_deadline_approaches() -> None:
    policy = AdaptiveMakerPolicy(
        MakerPolicyConfig(
            passive_guard_ticks=5,
            urgent_guard_ticks=2,
            max_passive_guard_ticks=20,
            volatility_guard_multiplier=2,
        )
    )
    market = replace(snapshot(), volatility_ticks=1)

    early = policy.decide(market, "sell", 0.01, 0)
    urgent = policy.decide(market, "sell", 0.01, 1)

    assert early.price == pytest.approx(market.ask + 5 * market.tick_size)
    assert urgent.price == pytest.approx(market.ask + 2 * market.tick_size)


def test_invalid_nonfinite_inputs_are_rejected() -> None:
    with pytest.raises(ValidationError):
        snapshot().__class__(
            timestamp_ms=0,
            bid=100,
            ask=101,
            bid_size=1,
            ask_size=1,
            buy_flow_per_sec=1,
            sell_flow_per_sec=1,
            volatility_ticks=float("nan"),
            tick_size=0.1,
        )
    with pytest.raises(ValidationError):
        TargetRequest("buy", float("nan"))
    with pytest.raises(ValidationError):
        TargetRequest("buy", 0.01, max_observation_errors=0)
    with pytest.raises(ValidationError):
        TargetRequest("buy", 0.01, max_cancel_verification_attempts=0)


def test_simulator_rejects_marketable_post_only_order() -> None:
    venue = fast_venue()
    market = venue.snapshot()
    order = venue.submit_post_only("buy", 0.01, market.ask, "would-cross")
    assert order.status == "rejected"
    assert order.filled_quantity == 0
    assert venue.position_quantity() == 0


def test_policy_cancels_stale_quote_but_holds_competitive_queue() -> None:
    policy = AdaptiveMakerPolicy(
        MakerPolicyConfig(min_rest_ms=100, max_rest_ms=1500, stale_ticks=1, adverse_threshold=1)
    )
    competitive = WorkingQuote("buy", 100, 0, 0.001, 0.005)
    stale = WorkingQuote("buy", 99.8, 0, 0.001, 0.005)

    assert policy.decide(snapshot(), "buy", 0.005, 0.5, competitive).action == "hold"
    decision = policy.decide(snapshot(), "buy", 0.005, 0.5, stale)
    assert decision.action == "cancel"
    assert decision.reason == "stale_price"


@dataclass
class CancelOncePolicy:
    config: MakerPolicyConfig = MakerPolicyConfig(min_rest_ms=100, max_rest_ms=10_000)
    canceled: bool = False

    def decide(
        self,
        market: MarketSnapshot,
        side: Side,
        remaining_quantity: float,
        urgency: float,
        working: WorkingQuote | None = None,
    ) -> QuoteDecision:
        if working is None:
            price = market.bid if side == "buy" else market.ask
            return QuoteDecision("quote", price, 1, 0, 0, "test_quote")
        if not self.canceled:
            self.canceled = True
            return QuoteDecision("cancel", None, 0, 0, 0, "test_partial_cancel")
        return QuoteDecision("hold", working.price, 1, 0, 0, "test_hold")


@dataclass
class ImmediateCancelPolicy:
    config: MakerPolicyConfig = MakerPolicyConfig(min_rest_ms=100, max_rest_ms=10_000)

    def decide(
        self,
        market: MarketSnapshot,
        side: Side,
        remaining_quantity: float,
        urgency: float,
        working: WorkingQuote | None = None,
    ) -> QuoteDecision:
        if working is None:
            price = market.bid if side == "buy" else market.ask
            return QuoteDecision("quote", price, 1, 0, 0, "test_quote")
        return QuoteDecision("cancel", None, 0, 0, 0, "test_cancel")


@dataclass
class AlwaysHoldPolicy:
    config: MakerPolicyConfig = MakerPolicyConfig(min_rest_ms=100, max_rest_ms=10_000)

    def decide(
        self,
        market: MarketSnapshot,
        side: Side,
        remaining_quantity: float,
        urgency: float,
        working: WorkingQuote | None = None,
    ) -> QuoteDecision:
        if working is None:
            price = market.bid if side == "buy" else market.ask
            return QuoteDecision("quote", price, 1, 0, 0, "test_quote")
        return QuoteDecision("hold", working.price, 1, 0, 0, "test_hold")


def fast_venue(seed: int = 7, *, fault: str | None = None) -> SimulatedMakerVenue:
    return SimulatedMakerVenue(
        seed,
        SimulationConfig(queue_factor=0.01, base_depth=0.001, base_flow_per_sec=0.05),
        fault=fault,
    )


def test_partial_fill_is_reconciled_before_cancel_and_target_finishes_exactly() -> None:
    venue = fast_venue()
    result = execute_adaptive_maker_target(
        venue,
        CancelOncePolicy(),
        TargetRequest("buy", 0.02, deadline_ms=10_000, poll_interval_ms=100),
    )

    assert result.status == "completed"
    assert result.cancels == 1
    assert result.fill_count >= 2
    assert result.quote_volume > 0
    assert abs(result.final_position - 0.02) <= 1e-9
    assert result.maker_only is True


def test_stop_request_cancels_the_active_maker_order_once_and_never_submits_another() -> None:
    class WaitingVenue:
        def __init__(self) -> None:
            self.time = 0
            self.position = 0.0
            self.order: VenueOrder | None = None
            self.submissions = 0
            self.cancel_calls = 0

        @property
        def now_ms(self) -> int:
            return self.time

        def snapshot(self) -> MarketSnapshot:
            return snapshot(timestamp_ms=self.time)

        def position_quantity(self) -> float:
            return self.position

        def wait_for_submission_slot(self) -> None:
            return None

        def submit_post_only(self, side: Side, quantity: float, price: float, client_order_id: str) -> VenueOrder:
            self.submissions += 1
            self.order = VenueOrder(
                client_order_id,
                client_order_id,
                side,
                price,
                quantity,
                0,
                0,
                "new",
                True,
                None,
            )
            return self.order

        def fetch_order(self, order_id: str, client_order_id: str) -> VenueOrder:
            assert self.order is not None
            return self.order

        def cancel_order(self, order_id: str, client_order_id: str) -> VenueOrder:
            self.cancel_calls += 1
            assert self.order is not None
            self.order = replace(self.order, status="canceled")
            return self.order

        def advance(self, milliseconds: int) -> None:
            self.time += milliseconds

    venue = WaitingVenue()
    stop = False

    def progress(event: dict[str, object]) -> None:
        nonlocal stop
        if event.get("event") == "submit":
            stop = True

    result = execute_adaptive_maker_target(
        venue,
        AlwaysHoldPolicy(),
        TargetRequest("buy", 0.02, deadline_ms=10_000, poll_interval_ms=100),
        progress_sink=progress,
        stop_requested=lambda: stop,
    )

    assert result.status == "stopped"
    assert result.reason == "stop_requested"
    assert venue.submissions == 1
    assert venue.cancel_calls == 1
    assert result.cancels == 1
    assert any(event["event"] == "stop_contained" for event in result.events)


def test_deadline_uses_symbol_wide_cleanup_and_stops_when_cleanup_is_uncertain() -> None:
    venue = fast_venue()
    cleanup_calls = 0

    def cleanup() -> bool:
        nonlocal cleanup_calls
        cleanup_calls += 1
        return False

    venue.cancel_all_and_verify = cleanup  # type: ignore[attr-defined]
    result = execute_adaptive_maker_target(
        venue,
        AlwaysHoldPolicy(),
        TargetRequest("buy", 0.02, deadline_ms=1, poll_interval_ms=1),
    )

    assert cleanup_calls == 1
    assert result.status == "uncertain"
    assert result.reason == "deadline_cleanup_not_confirmed"
    assert any(row["event"] == "timeout_cleanup_started" for row in result.events)


def test_progress_sink_receives_order_and_wait_events_during_execution() -> None:
    progress: list[dict[str, object]] = []

    result = execute_adaptive_maker_target(
        fast_venue(),
        AdaptiveMakerPolicy(MakerPolicyConfig()),
        TargetRequest("buy", 0.01, deadline_ms=10_000, poll_interval_ms=100),
        progress_sink=lambda event: progress.append(dict(event)),
    )

    assert result.status == "completed"
    assert progress == list(result.events)
    assert any(row["event"] == "submit" for row in progress)
    assert any(row["event"] == "fill" for row in progress)
    assert any(row["event"] == "wait" and row["waiting_for"] == "maker_fill" for row in progress)


def test_wait_heartbeats_are_rate_limited_during_slow_maker_fill() -> None:
    venue = SimulatedMakerVenue(
        7,
        SimulationConfig(queue_factor=1_000, base_depth=1, base_flow_per_sec=0.0001),
    )
    progress: list[dict[str, object]] = []

    execute_adaptive_maker_target(
        venue,
        AlwaysHoldPolicy(),
        TargetRequest("buy", 0.01, deadline_ms=4_500, poll_interval_ms=100, max_requotes=0),
        progress_sink=lambda event: progress.append(dict(event)),
    )

    waits = [row for row in progress if row["event"] == "wait" and row.get("waiting_for") == "maker_fill"]
    assert 2 <= len(waits) <= 3
    elapsed = [int(row["elapsed_ms"]) for row in waits]
    assert all(later - earlier >= 2_000 for earlier, later in zip(elapsed, elapsed[1:], strict=False))


def test_transient_unknown_order_state_recovers_without_resubmission() -> None:
    result = execute_adaptive_maker_target(
        fast_venue(fault="unknown"),
        AdaptiveMakerPolicy(MakerPolicyConfig()),
        TargetRequest("buy", 0.01),
    )

    assert result.status == "completed"
    assert 1 <= result.observation_errors < 12
    assert result.submissions == 1


def test_persistent_unknown_order_state_halts_without_resubmission() -> None:
    venue = fast_venue()
    original_fetch = venue.fetch_order

    def unknown_fetch(order_id: str, client_order_id: str):
        return replace(original_fetch(order_id, client_order_id), status="unknown")

    venue.fetch_order = unknown_fetch  # type: ignore[method-assign]
    result = execute_adaptive_maker_target(
        venue,
        AdaptiveMakerPolicy(MakerPolicyConfig()),
        TargetRequest("buy", 0.01),
    )

    assert result.status == "uncertain"
    assert result.reason == "cancel_not_confirmed"
    assert result.submissions == 1
    assert result.observation_errors == 12
    assert any(event["event"] == "observation_cleanup_not_confirmed" for event in result.events)


def test_transient_observation_error_retries_query_without_resubmission() -> None:
    venue = fast_venue()
    original_fetch = venue.fetch_order
    calls = 0

    def flaky_fetch(order_id: str, client_order_id: str):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ValidationError("temporary query failure")
        return original_fetch(order_id, client_order_id)

    venue.fetch_order = flaky_fetch  # type: ignore[method-assign]
    result = execute_adaptive_maker_target(
        venue,
        AdaptiveMakerPolicy(MakerPolicyConfig(adverse_threshold=1)),
        TargetRequest("buy", 0.01, deadline_ms=30_000),
    )

    assert result.status == "completed"
    assert result.observation_errors == 1
    assert result.submissions == 1


def test_transient_position_timeout_with_live_order_retries_without_resubmission() -> None:
    class Venue:
        def __init__(self) -> None:
            self._now = 0
            self.position = 0.0
            self.order = None
            self.position_timeout_pending = False
            self.submissions = 0
            self.cancels = 0

        @property
        def now_ms(self):
            return self._now

        def position_quantity(self):
            if self.position_timeout_pending:
                self.position_timeout_pending = False
                raise TimeoutError("temporary position timeout")
            return self.position

        def wait_for_submission_slot(self):
            return None

        def snapshot(self):
            return snapshot(timestamp_ms=self._now)

        def submit_post_only(self, side, quantity, price, client_order_id):
            self.submissions += 1
            self.position_timeout_pending = True
            self.order = VenueOrder(
                "position-timeout-1", client_order_id, side, price, quantity, 0, 0, "new", True, None
            )
            return self.order

        def fetch_order(self, order_id, client_order_id):
            if self.order.status == "new":
                self.position = self.order.quantity
                self.order = replace(
                    self.order,
                    filled_quantity=self.order.quantity,
                    cumulative_quote=self.order.quantity * self.order.price,
                    status="filled",
                    maker=True,
                )
            return self.order

        def cancel_order(self, order_id, client_order_id):
            self.cancels += 1
            raise AssertionError("a transient position timeout must not cancel the order")

        def advance(self, milliseconds):
            self._now += milliseconds

    venue = Venue()
    result = execute_adaptive_maker_target(
        venue,
        AdaptiveMakerPolicy(MakerPolicyConfig(adverse_threshold=1)),
        TargetRequest("buy", 0.01, deadline_ms=30_000),
    )

    assert result.status == "completed"
    assert result.submissions == 1
    assert venue.submissions == 1
    assert venue.cancels == 0
    assert any(event["event"] == "position_observation_error" for event in result.events)
    assert any(
        event["event"] == "wait" and event.get("waiting_for") == "position_observation_retry" for event in result.events
    )


def test_persistent_position_timeout_cancels_known_order_once_before_stopping() -> None:
    class Venue:
        def __init__(self) -> None:
            self._now = 0
            self.position = 0.0
            self.order = None
            self.position_calls = 0
            self.submissions = 0
            self.cancels = 0

        @property
        def now_ms(self):
            return self._now

        def position_quantity(self):
            self.position_calls += 1
            if self.order is not None:
                raise TimeoutError("persistent position timeout")
            return self.position

        def wait_for_submission_slot(self):
            return None

        def snapshot(self):
            return snapshot(timestamp_ms=self._now)

        def submit_post_only(self, side, quantity, price, client_order_id):
            self.submissions += 1
            self.order = VenueOrder(
                "persistent-timeout-1", client_order_id, side, price, quantity, 0, 0, "new", True, None
            )
            return self.order

        def fetch_order(self, order_id, client_order_id):
            return self.order

        def cancel_order(self, order_id, client_order_id):
            self.cancels += 1
            self.order = replace(self.order, status="canceled")
            return self.order

        def advance(self, milliseconds):
            self._now += milliseconds

    venue = Venue()
    result = execute_adaptive_maker_target(
        venue,
        AlwaysHoldPolicy(),
        TargetRequest("buy", 0.01, deadline_ms=30_000),
    )

    assert result.status == "uncertain"
    assert result.reason == "position_observation_unavailable"
    assert result.submissions == 1
    assert venue.submissions == 1
    assert venue.cancels == 1
    assert any(event["event"] == "observation_cleanup_confirmed" for event in result.events)


def test_successful_reads_reset_consecutive_observation_error_budget() -> None:
    class IntermittentVenue:
        def __init__(self) -> None:
            self._now = 0
            self.position = 0.0
            self.order = None
            self.fetch_calls = 0

        @property
        def now_ms(self):
            return self._now

        def position_quantity(self):
            return self.position

        def wait_for_submission_slot(self):
            return None

        def snapshot(self):
            return snapshot(timestamp_ms=self._now)

        def submit_post_only(self, side, quantity, price, client_order_id):
            self.order = VenueOrder("intermittent-1", client_order_id, side, price, quantity, 0, 0, "new", True, None)
            return self.order

        def fetch_order(self, order_id, client_order_id):
            self.fetch_calls += 1
            if self.fetch_calls in {1, 3, 5}:
                raise ValidationError("intermittent visibility failure")
            if self.fetch_calls == 6:
                self.position = self.order.quantity
                self.order = replace(
                    self.order,
                    filled_quantity=self.order.quantity,
                    cumulative_quote=self.order.quantity * self.order.price,
                    status="filled",
                    maker=True,
                )
            return self.order

        def cancel_order(self, order_id, client_order_id):
            raise AssertionError("intermittent observations must not submit a cancel")

        def advance(self, milliseconds):
            self._now += milliseconds

    result = execute_adaptive_maker_target(
        IntermittentVenue(),
        CancelOncePolicy(canceled=True),
        TargetRequest("buy", 0.01, deadline_ms=30_000, max_observation_errors=2),
    )

    assert result.status == "completed"
    assert result.observation_errors == 3
    assert result.submissions == 1


def test_submission_throttle_wait_happens_before_quote_snapshot() -> None:
    class DelayedVenue:
        def __init__(self) -> None:
            self._now = 0
            self.position = 0.0
            self.order = None
            self.waited = False

        @property
        def now_ms(self):
            return self._now

        def position_quantity(self):
            return self.position

        def wait_for_submission_slot(self):
            self._now += 10_100
            self.waited = True

        def snapshot(self):
            bid = 101.0 if self.waited else 100.0
            return snapshot(timestamp_ms=self._now, bid=bid, ask=bid + 0.1)

        def submit_post_only(self, side, quantity, price, client_order_id):
            assert self.waited is True
            assert price == pytest.approx(101.0)
            self.position = quantity
            self.order = VenueOrder(
                "fresh-1", client_order_id, side, price, quantity, quantity, quantity * price, "filled", True, True
            )
            return self.order

        def fetch_order(self, order_id, client_order_id):
            return self.order

        def cancel_order(self, order_id, client_order_id):
            raise AssertionError("filled order must not be canceled")

        def advance(self, milliseconds):
            self._now += milliseconds

    result = execute_adaptive_maker_target(
        DelayedVenue(),
        AdaptiveMakerPolicy(MakerPolicyConfig(adverse_threshold=1)),
        TargetRequest("buy", 0.01, deadline_ms=30_000),
    )

    assert result.status == "completed"
    assert result.submissions == 1


def test_local_preflight_skip_requotes_without_counting_submission() -> None:
    class SkipOnceVenue:
        def __init__(self) -> None:
            self._now = 0
            self.position = 0.0
            self.skipped = False
            self.order = None

        @property
        def now_ms(self):
            return self._now

        def position_quantity(self):
            return self.position

        def wait_for_submission_slot(self):
            return None

        def snapshot(self):
            return snapshot(timestamp_ms=self._now)

        def submit_post_only(self, side, quantity, price, client_order_id):
            if not self.skipped:
                self.skipped = True
                return VenueOrder(
                    "",
                    client_order_id,
                    side,
                    price,
                    quantity,
                    0,
                    0,
                    "not_submitted",
                    True,
                    None,
                    cancellation_reason="LOCAL_PRICE_WOULD_TAKE",
                )
            self.position = quantity
            self.order = VenueOrder(
                "accepted-1",
                client_order_id,
                side,
                price,
                quantity,
                quantity,
                quantity * price,
                "filled",
                True,
                True,
            )
            return self.order

        def fetch_order(self, order_id, client_order_id):
            return self.order

        def cancel_order(self, order_id, client_order_id):
            raise AssertionError("filled order must not be canceled")

        def advance(self, milliseconds):
            self._now += milliseconds

    result = execute_adaptive_maker_target(
        SkipOnceVenue(),
        AdaptiveMakerPolicy(MakerPolicyConfig(adverse_threshold=1)),
        TargetRequest("buy", 0.01, deadline_ms=10_000),
    )

    assert result.status == "completed"
    assert result.preflight_skips == 1
    assert result.submissions == 1
    assert any(event["event"] == "preflight_skip" for event in result.events)


def test_deadline_cancel_fill_is_reconciled_as_target_reached() -> None:
    class FillOnCancelVenue:
        def __init__(self) -> None:
            self._now = 0
            self.position = 0.01
            self.order = None

        @property
        def now_ms(self):
            return self._now

        def position_quantity(self):
            return self.position

        def wait_for_submission_slot(self):
            return None

        def snapshot(self):
            return snapshot(timestamp_ms=self._now)

        def submit_post_only(self, side, quantity, price, client_order_id):
            self.order = VenueOrder("close-1", client_order_id, side, price, quantity, 0, 0, "new", True, None)
            return self.order

        def fetch_order(self, order_id, client_order_id):
            return self.order

        def cancel_order(self, order_id, client_order_id):
            self.position = 0
            self.order = replace(
                self.order,
                filled_quantity=self.order.quantity,
                cumulative_quote=self.order.quantity * self.order.price,
                status="filled",
                maker=True,
            )
            return self.order

        def advance(self, milliseconds):
            self._now += milliseconds

    result = execute_adaptive_maker_target(
        FillOnCancelVenue(),
        AdaptiveMakerPolicy(MakerPolicyConfig(min_rest_ms=2_000, max_rest_ms=10_000, adverse_threshold=1)),
        TargetRequest("sell", 0, deadline_ms=1_000, poll_interval_ms=100),
    )

    assert result.status == "completed"
    assert result.reason == "target_reached"
    assert result.final_position == 0


def test_exchange_could_not_fill_is_terminal_post_only_rejection() -> None:
    venue = fast_venue()
    original_fetch = venue.fetch_order

    def rejected_fetch(order_id: str, client_order_id: str):
        order = original_fetch(order_id, client_order_id)
        return order.__class__(
            order.order_id,
            order.client_order_id,
            order.side,
            order.price,
            order.quantity,
            0,
            0,
            "canceled",
            True,
            None,
            order.queue_ahead,
            "COULD_NOT_FILL",
        )

    venue.fetch_order = rejected_fetch  # type: ignore[method-assign]
    result = execute_adaptive_maker_target(
        venue,
        AdaptiveMakerPolicy(MakerPolicyConfig(adverse_threshold=1)),
        TargetRequest("buy", 0.01),
    )

    assert result.status == "failed"
    assert result.reason == "post_only_rejected"
    assert result.submissions == 1
    assert result.post_only_rejections == 1
    assert result.venue_cancels == 1


def test_taker_fill_is_a_hard_failure() -> None:
    result = execute_adaptive_maker_target(
        fast_venue(fault="taker_fill"),
        AdaptiveMakerPolicy(MakerPolicyConfig(adverse_threshold=1)),
        TargetRequest("buy", 0.01),
    )
    assert result.status == "failed"
    assert result.reason == "taker_fill_detected"
    assert result.maker_only is False


def test_unconfirmed_cancel_halts_as_uncertain() -> None:
    result = execute_adaptive_maker_target(
        fast_venue(fault="unconfirmed_cancel"),
        ImmediateCancelPolicy(),
        TargetRequest("buy", 0.05),
    )
    assert result.status == "uncertain"
    assert result.reason == "cancel_not_confirmed"
    assert result.submissions == 1
    assert result.cancel_verification_attempts == 5


def test_cancel_verification_polls_until_history_reaches_terminal_state() -> None:
    venue = fast_venue()
    original_cancel = venue.cancel_order
    original_fetch = venue.fetch_order
    cancel_started = False
    delayed_reads = 0

    def delayed_cancel(order_id: str, client_order_id: str):
        nonlocal cancel_started
        canceled = original_cancel(order_id, client_order_id)
        cancel_started = True
        return replace(canceled, status="unknown")

    def delayed_fetch(order_id: str, client_order_id: str):
        nonlocal delayed_reads
        order = original_fetch(order_id, client_order_id)
        if cancel_started and delayed_reads < 2:
            delayed_reads += 1
            return replace(order, status="unknown")
        return order

    venue.cancel_order = delayed_cancel  # type: ignore[method-assign]
    venue.fetch_order = delayed_fetch  # type: ignore[method-assign]
    result = execute_adaptive_maker_target(
        venue,
        CancelOncePolicy(),
        TargetRequest("buy", 0.02, deadline_ms=10_000, poll_interval_ms=100),
    )

    assert result.status == "completed"
    assert result.cancels == 1
    assert result.cancel_verification_attempts >= 3
    assert result.cancel_verification_errors == 0
    assert any(event["event"] == "cancel_verification" and event["attempt"] == 3 for event in result.events)


def test_cancel_request_error_is_reconciled_without_second_cancel_submission() -> None:
    venue = fast_venue()
    original_cancel = venue.cancel_order
    cancel_calls = 0

    def accepted_then_disconnected(order_id: str, client_order_id: str):
        nonlocal cancel_calls
        cancel_calls += 1
        original_cancel(order_id, client_order_id)
        raise ValidationError("connection lost after cancel acceptance")

    venue.cancel_order = accepted_then_disconnected  # type: ignore[method-assign]
    result = execute_adaptive_maker_target(
        venue,
        CancelOncePolicy(),
        TargetRequest("buy", 0.02, deadline_ms=10_000, poll_interval_ms=100),
    )

    assert result.status == "completed"
    assert cancel_calls == 1
    assert result.cancel_verification_errors == 1
    assert any(event["event"] == "cancel_request_error" for event in result.events)


def test_absent_order_with_unchanged_position_is_safely_reconciled() -> None:
    venue = fast_venue()
    original_cancel = venue.cancel_order
    original_fetch = venue.fetch_order
    canceled_order_id = ""

    def absent_cancel(order_id: str, client_order_id: str):
        nonlocal canceled_order_id
        canceled = original_cancel(order_id, client_order_id)
        canceled_order_id = order_id
        return replace(canceled, status="unknown", cancellation_reason="OPEN_ORDER_ABSENT")

    def lagging_fetch(order_id: str, client_order_id: str):
        order = original_fetch(order_id, client_order_id)
        if order_id == canceled_order_id:
            return replace(order, status="unknown", cancellation_reason="OPEN_ORDER_ABSENT")
        return order

    venue.cancel_order = absent_cancel  # type: ignore[method-assign]
    venue.fetch_order = lagging_fetch  # type: ignore[method-assign]
    result = execute_adaptive_maker_target(
        venue,
        CancelOncePolicy(),
        TargetRequest("buy", 0.02, deadline_ms=30_000, poll_interval_ms=100),
    )

    assert result.status == "completed"
    assert result.cancel_verification_attempts >= 1
    assert any(event["event"] == "cancel_reconciled_absent" for event in result.events)


def test_absent_order_is_not_reconciled_when_position_changed() -> None:
    venue = fast_venue(fault="unconfirmed_cancel")
    original_cancel = venue.cancel_order

    def changed_position_cancel(order_id: str, client_order_id: str):
        canceled = original_cancel(order_id, client_order_id)
        venue._position += 0.001
        return replace(canceled, status="unknown", cancellation_reason="OPEN_ORDER_ABSENT")

    venue.cancel_order = changed_position_cancel  # type: ignore[method-assign]
    result = execute_adaptive_maker_target(
        venue,
        ImmediateCancelPolicy(),
        TargetRequest("buy", 0.05),
    )

    assert result.status == "uncertain"
    assert result.reason == "cancel_not_confirmed"
    assert not any(event["event"] == "cancel_reconciled_absent" for event in result.events)


def test_absent_cancel_response_is_not_enough_when_order_still_reads_active() -> None:
    venue = fast_venue(fault="unconfirmed_cancel")
    original_cancel = venue.cancel_order

    def stale_absent_response(order_id: str, client_order_id: str):
        order = original_cancel(order_id, client_order_id)
        return replace(order, status="unknown", cancellation_reason="OPEN_ORDER_ABSENT")

    venue.cancel_order = stale_absent_response  # type: ignore[method-assign]
    result = execute_adaptive_maker_target(
        venue,
        ImmediateCancelPolicy(),
        TargetRequest("buy", 0.05),
    )

    assert result.status == "uncertain"
    assert result.reason == "cancel_not_confirmed"
    assert not any(event["event"] == "cancel_reconciled_absent" for event in result.events)


def test_fill_observed_at_deadline_is_still_completed() -> None:
    venue = fast_venue()
    original_fetch = venue.fetch_order

    def fetch_after_deadline(order_id: str, client_order_id: str):
        order = original_fetch(order_id, client_order_id)
        if order.status == "filled":
            venue.advance(10_000)
        return order

    venue.fetch_order = fetch_after_deadline  # type: ignore[method-assign]
    result = execute_adaptive_maker_target(
        venue,
        AdaptiveMakerPolicy(MakerPolicyConfig(max_rest_ms=20_000, adverse_threshold=1)),
        TargetRequest("buy", 0.001, deadline_ms=10_000, poll_interval_ms=100),
    )

    assert result.status == "completed"
    assert result.reason == "target_reached"


def test_benchmark_reaches_10000_with_five_cycles_and_beats_fixed_baseline() -> None:
    report = run_benchmark(BenchmarkConfig(train_trials=5, validation_trials=5))

    assert report["status"] == "passed"
    assert all(report["acceptance"].values())
    assert report["adaptive"]["minimum_volume"] >= 10_000
    assert report["adaptive"]["minimum_maker_fills"] >= 10
    assert report["adaptive"]["maximum_overfill"] <= 1e-9
    assert len(report["validation_trials"]) == 5
    assert all(trial["cycles_completed"] >= 5 for trial in report["validation_trials"])
    assert all(abs(trial["final_position"]) <= 1e-9 for trial in report["validation_trials"])


def test_trial_accepts_custom_market_scenario() -> None:
    result = run_trial(
        lambda: AdaptiveMakerPolicy(MakerPolicyConfig(adverse_threshold=1)),
        BenchmarkConfig(train_trials=5, validation_trials=5, per_leg_deadline_ms=120_000),
        123,
        simulation_config=SimulationConfig(base_depth=0.006, base_flow_per_sec=0.02),
    )

    assert result.maker_only is True
    assert result.post_only_rejections == 0
