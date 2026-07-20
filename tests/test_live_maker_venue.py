from __future__ import annotations

from decimal import Decimal

import ccxt
import pytest

from weex_cli.live_maker_venue import LiveAdaptiveMakerVenue
from weex_cli.live_websocket import MarketStreamUnavailable


class Gateway:
    def __init__(self) -> None:
        self.position_rows: list[dict] = []
        self.history: list[dict] = []
        self.intents = []
        self.history_calls = 0
        self.open_order_calls = 0
        self.order_book_calls = 0

    def order_book(self, symbol: str, limit: int = 5) -> dict:
        self.order_book_calls += 1
        return {"bids": [[49, 10], [48, 10]], "asks": [[51, 10], [52, 10]]}

    def positions(self, mode: str, symbol: str | None = None) -> list[dict]:
        return list(self.position_rows)

    def amount_to_precision(self, symbol: str, amount: Decimal) -> Decimal:
        return amount

    def price_to_precision(self, symbol: str, price: Decimal) -> Decimal:
        return price

    def place_order(self, intent) -> dict:
        self.intents.append(intent)
        row = {
            "id": f"order-{len(self.intents)}",
            "clientOrderId": intent.client_order_id,
            "side": intent.side,
            "status": "open",
            "amount": float(intent.quantity),
            "filled": 0,
            "price": float(intent.price),
            "timeInForce": "POST_ONLY",
            "postOnly": True,
        }
        self.history = [row]
        return row

    def open_orders(self, symbol: str | None = None, *, mode: str = "live", trigger: bool = False) -> list[dict]:
        self.open_order_calls += 1
        return list(self.history)

    def order_history(self, mode: str, symbol: str | None = None, limit: int = 100) -> list[dict]:
        self.history_calls += 1
        return list(self.history)

    def cancel_order(self, symbol: str, order_id: str, *, mode: str = "live") -> dict:
        row = {**self.history[0], "status": "canceled"}
        self.history = [row]
        return row


class TimeoutCleanupGateway(Gateway):
    def __init__(self, *, conditional_rows: list[dict] | None = None, cancel_conditional: bool = True) -> None:
        super().__init__()
        self.conditional_rows = list(conditional_rows or [])
        self.cancel_conditional = cancel_conditional
        self.cancel_all_calls: list[bool] = []

    def cancel_all_orders(self, symbol: str, *, trigger: bool = False, mode: str = "live") -> list[dict]:
        self.cancel_all_calls.append(trigger)
        if trigger:
            if self.cancel_conditional:
                self.conditional_rows.clear()
        else:
            self.history.clear()
        return []

    def algo_orders(self, symbol: str | None = None) -> list[dict]:
        return list(self.conditional_rows)


class FlakyTimeoutCleanupGateway(TimeoutCleanupGateway):
    def __init__(self) -> None:
        super().__init__(conditional_rows=[{"id": "algo-1"}])
        self.regular_read_failures = 1

    def open_orders(self, symbol: str | None = None, *, mode: str = "live", trigger: bool = False) -> list[dict]:
        if self.regular_read_failures:
            self.regular_read_failures -= 1
            raise ccxt.RequestTimeout("temporary open-order timeout")
        return super().open_orders(symbol, mode=mode, trigger=trigger)

    def cancel_all_orders(self, symbol: str, *, trigger: bool = False, mode: str = "live") -> list[dict]:
        super().cancel_all_orders(symbol, trigger=trigger, mode=mode)
        raise ccxt.RequestTimeout("cancel response lost")


class SparseCreateGateway(Gateway):
    def place_order(self, intent) -> dict:
        self.intents.append(intent)
        self.history = [
            {
                "id": "order-1",
                "clientOrderId": intent.client_order_id,
                "side": intent.side,
                "status": "open",
                "amount": float(intent.quantity),
                "filled": 0,
                "price": float(intent.price),
                "timeInForce": "POST_ONLY",
                "postOnly": False,
            }
        ]
        return {
            "id": "order-1",
            "clientOrderId": intent.client_order_id,
            "side": intent.side,
            "status": "open",
            "amount": float(intent.quantity),
            "price": float(intent.price),
        }


class ZeroQuantityCreateGateway(Gateway):
    def place_order(self, intent) -> dict:
        self.intents.append(intent)
        return {
            "id": "order-1",
            "clientOrderId": intent.client_order_id,
            "side": intent.side,
            "status": "open",
            "amount": 0,
            "price": 0,
            "timeInForce": "POST_ONLY",
        }


class CachedMarketData:
    def order_book(self, symbol: str, limit: int = 5) -> dict:
        return {"bids": [[60, 5], [59, 5]], "asks": [[61, 5], [62, 5]]}


class UnavailableMarketData:
    def order_book(self, symbol: str, limit: int = 5) -> dict:
        raise MarketStreamUnavailable("disconnected")


class OrderUpdates:
    def order_update(self, order_id: str, client_order_id: str) -> dict:
        return {
            "id": order_id,
            "clientOrderId": client_order_id,
            "side": "buy",
            "status": "filled",
            "amount": 1,
            "filled": 1,
            "cost": 49,
            "price": 49,
            "timeInForce": "POST_ONLY",
        }


def test_short_position_is_signed_and_wrong_side_fails_closed() -> None:
    gateway = Gateway()
    venue = LiveAdaptiveMakerVenue(gateway, "ETH", "short", clock=lambda: 0, sleep=lambda _: None)  # type: ignore[arg-type]

    gateway.position_rows = [{"side": "short", "contracts": "1.25"}]
    assert venue.position_quantity() == -1.25

    gateway.position_rows = [{"side": "long", "contracts": "1.25"}]
    with pytest.raises(Exception, match="unexpected long ETH position"):
        venue.position_quantity()


def test_live_venue_preserves_post_only_and_maps_short_open_close_directions() -> None:
    gateway = Gateway()
    venue = LiveAdaptiveMakerVenue(gateway, "ETH", "short", clock=lambda: 0, sleep=lambda _: None)  # type: ignore[arg-type]

    opened = venue.submit_post_only("sell", 1, 51, "short-open")
    assert opened.post_only is True
    assert gateway.intents[-1].position_side == "short"
    assert gateway.intents[-1].reduce_only is False

    gateway.position_rows = [{"side": "short", "contracts": "1"}]
    closed = venue.submit_post_only("buy", 1, 49, "short-close")
    assert closed.post_only is True
    assert gateway.intents[-1].reduce_only is True


def test_cancel_is_submitted_once_and_terminal_state_is_returned() -> None:
    gateway = Gateway()
    venue = LiveAdaptiveMakerVenue(gateway, "ETH", "short", clock=lambda: 0, sleep=lambda _: None)  # type: ignore[arg-type]
    opened = venue.submit_post_only("sell", 1, 51, "short-open")

    canceled = venue.cancel_order(opened.order_id, opened.client_order_id)

    assert canceled.status == "canceled"
    assert canceled.post_only is True


def test_fetch_order_returns_active_match_without_querying_history() -> None:
    gateway = Gateway()
    venue = LiveAdaptiveMakerVenue(gateway, "BTC", "long", clock=lambda: 0, sleep=lambda _: None)  # type: ignore[arg-type]
    opened = venue.submit_post_only("buy", 1, 49, "btc-open")
    history_calls_after_submit = gateway.history_calls

    observed = venue.fetch_order(opened.order_id, opened.client_order_id)

    assert observed.order_id == opened.order_id
    assert observed.status == "new"
    assert gateway.history_calls == history_calls_after_submit


def test_live_venue_skips_stale_price_that_is_not_current_same_side_bbo() -> None:
    gateway = Gateway()
    venue = LiveAdaptiveMakerVenue(gateway, "BTC", "long", clock=lambda: 0, sleep=lambda _: None)  # type: ignore[arg-type]

    order = venue.submit_post_only("buy", 1, 48, "stale-btc-open")

    assert order.status == "not_submitted"
    assert order.cancellation_reason == "LOCAL_NOT_SAME_SIDE_BBO"
    assert gateway.intents == []


def test_sparse_create_response_is_reconciled_by_client_order_id() -> None:
    gateway = SparseCreateGateway()
    venue = LiveAdaptiveMakerVenue(gateway, "BTC", "long", clock=lambda: 0, sleep=lambda _: None)  # type: ignore[arg-type]

    order = venue.submit_post_only("buy", 0.0003, 49, "btc-open")

    assert order.status == "new"
    assert order.post_only is True
    assert order.client_order_id == "btc-open"


def test_sparse_zero_quantity_response_uses_submitted_values_for_progress() -> None:
    gateway = ZeroQuantityCreateGateway()
    venue = LiveAdaptiveMakerVenue(gateway, "BTC", "long", clock=lambda: 0, sleep=lambda _: None)  # type: ignore[arg-type]

    order = venue.submit_post_only("buy", 0.0028, 49, "btc-open")

    assert order.quantity == pytest.approx(0.0028)
    assert order.price == pytest.approx(49)


def test_live_venue_prefers_websocket_book_and_falls_back_to_rest() -> None:
    gateway = Gateway()
    websocket_venue = LiveAdaptiveMakerVenue(gateway, "BTC", "long", market_data=CachedMarketData())  # type: ignore[arg-type]

    assert websocket_venue.snapshot().bid == 60
    assert gateway.order_book_calls == 0

    fallback_venue = LiveAdaptiveMakerVenue(gateway, "BTC", "long", market_data=UnavailableMarketData())  # type: ignore[arg-type]
    assert fallback_venue.snapshot().bid == 49
    assert gateway.order_book_calls == 1


def test_live_venue_uses_private_order_update_before_rest_polling() -> None:
    gateway = Gateway()
    venue = LiveAdaptiveMakerVenue(gateway, "BTC", "long", order_updates=OrderUpdates())  # type: ignore[arg-type]

    order = venue.fetch_order("order-1", "client-1")

    assert order.status == "filled"
    assert order.post_only is True
    assert gateway.open_order_calls == 0
    assert gateway.history_calls == 0


def test_timeout_cleanup_cancels_regular_and_conditional_orders_once_and_verifies_empty() -> None:
    gateway = TimeoutCleanupGateway(conditional_rows=[{"id": "algo-1"}])
    gateway.history = [{"id": "regular-1", "status": "open"}]
    venue = LiveAdaptiveMakerVenue(gateway, "BTC", "long", clock=lambda: 0, sleep=lambda _: None)  # type: ignore[arg-type]

    assert venue.cancel_all_and_verify(max_attempts=2) is True
    assert gateway.cancel_all_calls == [False, True]


def test_timeout_cleanup_reports_uncertain_when_conditional_order_remains() -> None:
    gateway = TimeoutCleanupGateway(conditional_rows=[{"id": "algo-1"}], cancel_conditional=False)
    venue = LiveAdaptiveMakerVenue(gateway, "BTC", "long", clock=lambda: 0, sleep=lambda _: None)  # type: ignore[arg-type]

    assert venue.cancel_all_and_verify(max_attempts=2) is False
    assert gateway.cancel_all_calls == [False, True]


def test_timeout_cleanup_recovers_read_timeout_without_repeating_cancel_mutations() -> None:
    gateway = FlakyTimeoutCleanupGateway()
    gateway.history = [{"id": "regular-1", "status": "open"}]
    delays: list[float] = []
    venue = LiveAdaptiveMakerVenue(gateway, "BTC", "long", clock=lambda: 0, sleep=delays.append)  # type: ignore[arg-type]

    assert venue.cancel_all_and_verify(max_attempts=3) is True
    assert gateway.cancel_all_calls == [False, True]
    assert delays == [0.25]
