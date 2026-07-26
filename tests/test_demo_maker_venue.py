from __future__ import annotations

from decimal import ROUND_DOWN, Decimal

import pytest

from weex_cli.core.errors import SubmissionUncertainError, ValidationError
from weex_cli.execution.venues import DemoAdaptiveMakerVenue


class Clock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds


class VenueGateway:
    def __init__(self) -> None:
        self.position = Decimal("0.0160")
        self.active: list[dict] = []
        self.history: list[dict] = []
        self.next_id = 1
        self.open_calls = 0
        self.web_history_calls = 0

    def order_book(self, symbol, limit):
        return {"bids": [[100.0, 2], [99.9, 1]], "asks": [[100.1, 3], [100.2, 1]]}

    def positions(self, mode, symbol=None):
        return [] if self.position == 0 else [{"side": "LONG", "size": str(self.position)}]

    def amount_to_precision(self, symbol, amount):
        return Decimal(str(amount)).quantize(Decimal("0.0001"), rounding=ROUND_DOWN)

    def price_to_precision(self, symbol, price):
        return Decimal(str(price)).quantize(Decimal("0.1"))

    def amount_step(self, symbol):
        return Decimal("0.0001")

    def place_order(self, intent):
        order_id = str(self.next_id)
        self.next_id += 1
        self.active.append(
            {
                "id": order_id,
                "clientOrderId": intent.client_order_id,
                "orderSide": intent.side.upper(),
                "price": str(intent.price),
                "size": str(intent.quantity),
                "cumFillSize": "0",
                "cumFillValue": "0",
                "status": "OPEN",
                "timeInForce": "POST_ONLY",
            }
        )
        return {"orderId": order_id, "clientOrderId": intent.client_order_id, "success": True}

    def order_history(self, mode, symbol=None, limit=100, start_time=None, end_time=None):
        return self.history

    def demo_web_order_history(self, symbol=None, limit=100):
        self.web_history_calls += 1
        return self.history[:limit]

    def open_orders(self, symbol=None, *, trigger=False, mode="live"):
        self.open_calls += 1
        return list(self.active)

    def cancel_order(self, symbol, order_id, *, trigger=False, mode="live"):
        row = next(row for row in self.active if row["id"] == order_id)
        self.active.remove(row)
        self.history.insert(
            0,
            {
                "orderId": row["id"],
                "clientOrderId": row["clientOrderId"],
                "side": row["orderSide"],
                "price": row["price"],
                "origQty": row["size"],
                "executedQty": "0",
                "cumQuote": "0",
                "status": "CANCELED",
                "timeInForce": "POST_ONLY",
            },
        )
        return {"status": "verified_canceled"}


def test_real_venue_maps_book_position_submit_query_and_cancel() -> None:
    gateway = VenueGateway()
    clock = Clock()
    venue = DemoAdaptiveMakerVenue(gateway, "BTC", clock=clock, sleep=clock.sleep)

    market = venue.snapshot()
    assert market.bid == 100 and market.ask == 100.1 and market.tick_size == pytest_approx(0.1)
    assert venue.position_quantity() == 0.016

    order = venue.submit_post_only("sell", 0.016, 100.1, "first")
    assert order.status == "new"
    assert venue.fetch_order(order.order_id, order.client_order_id).post_only is True
    open_calls_before_cancel = gateway.open_calls
    canceled = venue.cancel_order(order.order_id, order.client_order_id)
    assert canceled.status == "unknown"
    assert canceled.cancellation_reason == "OPEN_ORDER_ABSENT"
    assert gateway.open_calls == open_calls_before_cancel
    terminal = venue.fetch_order(order.order_id, order.client_order_id)
    assert terminal.status == "unknown"
    assert terminal.cancellation_reason == "CANCELED_REASON_UNKNOWN"

    venue.submit_post_only("sell", 0.016, 100.2, "second")
    assert clock.value >= 110.1


def test_open_order_queries_are_throttled_while_v3_history_is_empty() -> None:
    gateway = VenueGateway()
    clock = Clock()
    venue = DemoAdaptiveMakerVenue(gateway, "BTC", clock=clock, sleep=clock.sleep)
    order = venue.submit_post_only("sell", 0.016, 100.1, "throttled-open")

    assert venue.fetch_order(order.order_id, order.client_order_id).status == "new"
    assert gateway.open_calls == 1
    assert venue.fetch_order(order.order_id, order.client_order_id).status == "new"
    assert gateway.open_calls == 1

    clock.value += 3.1
    assert venue.fetch_order(order.order_id, order.client_order_id).status == "new"
    assert gateway.open_calls == 2


def test_web_failure_starts_cooldown_without_repeating_open_order_call() -> None:
    gateway = VenueGateway()
    clock = Clock()
    venue = DemoAdaptiveMakerVenue(gateway, "BTC", clock=clock, sleep=clock.sleep)
    order = venue.submit_post_only("sell", 0.016, 100.1, "cooldown-open")
    calls = 0

    def unavailable_open_orders(symbol=None, *, trigger=False, mode="live"):
        nonlocal calls
        calls += 1
        raise SubmissionUncertainError("open-order gateway unavailable")

    gateway.open_orders = unavailable_open_orders
    first = venue.fetch_order(order.order_id, order.client_order_id)
    second = venue.fetch_order(order.order_id, order.client_order_id)

    assert first.status == "unknown" and second.status == "unknown"
    assert first.cancellation_reason == "WEB_VISIBILITY_COOLDOWN"
    assert calls == 1


def test_history_could_not_fill_reason_is_preserved() -> None:
    gateway = VenueGateway()
    gateway.history.append(
        {
            "orderId": "rejected-1",
            "clientOrderId": "rejected-client",
            "side": "SELL",
            "price": "100.1",
            "origQty": "0.016",
            "executedQty": "0",
            "cumQuote": "0",
            "status": "CANCELED",
            "cancelReason": "COULD_NOT_FILL",
            "timeInForce": "POST_ONLY",
        }
    )
    venue = DemoAdaptiveMakerVenue(gateway, "BTC")

    order = venue.fetch_order("rejected-1", "rejected-client")

    assert order.status == "canceled"
    assert order.cancellation_reason == "COULD_NOT_FILL"


def test_submission_normalizes_float_price_to_market_step() -> None:
    gateway = VenueGateway()
    venue = DemoAdaptiveMakerVenue(gateway, "BTC")

    order = venue.submit_post_only("sell", 0.016, 63127.09999999999, "precise-price")

    assert order.price == 63127.1
    assert gateway.active[0]["price"] == "63127.1"


def test_submission_preflight_skips_stale_price_without_calling_order_api() -> None:
    gateway = VenueGateway()
    venue = DemoAdaptiveMakerVenue(gateway, "BTC")

    order = venue.submit_post_only("sell", 0.016, 99.9, "stale-price")

    assert order.status == "not_submitted"
    assert order.cancellation_reason == "LOCAL_PRICE_WOULD_TAKE"
    assert gateway.active == []


def test_submission_preflight_book_error_is_not_an_order_attempt() -> None:
    gateway = VenueGateway()

    def unavailable_book(symbol, limit):
        raise ValidationError("temporary book error")

    gateway.order_book = unavailable_book
    venue = DemoAdaptiveMakerVenue(gateway, "BTC")

    order = venue.submit_post_only("sell", 0.016, 100.6, "book-unavailable")

    assert order.status == "not_submitted"
    assert order.cancellation_reason == "LOCAL_BOOK_UNAVAILABLE:ValidationError"
    assert gateway.active == []


def test_v3_filled_history_is_safe_fallback_when_web_history_is_rate_limited() -> None:
    gateway = VenueGateway()
    gateway.history.append(
        {
            "orderId": "filled-1",
            "clientOrderId": "filled-client",
            "side": "SELL",
            "price": "100.1",
            "origQty": "0.016",
            "executedQty": "0.016",
            "cumQuote": "1.6016",
            "status": "FILLED",
            "timeInForce": "POST_ONLY",
        }
    )

    def rate_limited(symbol=None, limit=100):
        raise ValidationError("rate limited")

    def unavailable_open_orders(symbol=None, *, trigger=False, mode="live"):
        raise SubmissionUncertainError("open-order gateway unavailable")

    gateway.open_orders = unavailable_open_orders
    gateway.demo_web_order_history = rate_limited
    order = DemoAdaptiveMakerVenue(gateway, "BTC").fetch_order("filled-1", "filled-client")

    assert order.status == "filled"
    assert order.maker is True


def test_open_order_failure_without_terminal_history_remains_uncertain() -> None:
    gateway = VenueGateway()

    def unavailable_open_orders(symbol=None, *, trigger=False, mode="live"):
        raise SubmissionUncertainError("open-order gateway unavailable")

    gateway.open_orders = unavailable_open_orders

    with pytest.raises(SubmissionUncertainError, match="open-order visibility"):
        DemoAdaptiveMakerVenue(gateway, "BTC").fetch_order("missing-1", "missing-client")


def test_absent_open_order_without_history_is_retryable_unknown() -> None:
    order = DemoAdaptiveMakerVenue(VenueGateway(), "BTC").fetch_order("missing-1", "missing-client")

    assert order.status == "unknown"
    assert order.cancellation_reason == "OPEN_ORDER_ABSENT"


def test_v3_cancel_without_reason_is_unknown_when_web_history_is_unavailable() -> None:
    gateway = VenueGateway()
    gateway.history.append(
        {
            "orderId": "canceled-1",
            "clientOrderId": "canceled-client",
            "side": "SELL",
            "price": "100.1",
            "origQty": "0.016",
            "executedQty": "0",
            "cumQuote": "0",
            "status": "CANCELED",
            "timeInForce": "POST_ONLY",
        }
    )

    def rate_limited(symbol=None, limit=100):
        raise ValidationError("rate limited")

    gateway.demo_web_order_history = rate_limited
    order = DemoAdaptiveMakerVenue(gateway, "BTC").fetch_order("canceled-1", "canceled-client")

    assert order.status == "unknown"


def test_verified_absence_is_preserved_when_cancel_history_is_delayed() -> None:
    gateway = VenueGateway()
    venue = DemoAdaptiveMakerVenue(gateway, "BTC")
    order = venue.submit_post_only("sell", 0.016, 100.1, "delayed-cancel-history")

    def rate_limited(symbol=None, limit=100):
        raise ValidationError("rate limited")

    gateway.demo_web_order_history = rate_limited
    canceled = venue.cancel_order(order.order_id, order.client_order_id)

    assert gateway.active == []
    assert canceled.status == "unknown"
    assert canceled.cancellation_reason == "OPEN_ORDER_ABSENT"


def pytest_approx(value: float):
    import pytest

    return pytest.approx(value)
