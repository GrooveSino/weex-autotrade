from __future__ import annotations

import ccxt
import pytest

from weex_cli.errors import SafetyError, SubmissionUncertainError
from weex_cli.models import OrderIntent
from weex_cli.service import TradingService


def intent(mode: str = "demo") -> OrderIntent:
    return OrderIntent.create(
        mode=mode,
        symbol="BTC",
        side="buy",
        position_side="long",
        order_type="limit",
        quantity="0.001",
        price="60000",
        client_order_id="client-1",
    )


class FakeGateway:
    def __init__(self) -> None:
        self.positions_rows = []
        self.open_rows = []
        self.history_rows = []
        self.place_result = {"success": True}
        self.events = []

    def positions(self, mode, symbol=None):
        self.events.append(("positions", mode, symbol))
        return self.positions_rows

    def open_orders(self, symbol=None):
        self.events.append(("open_orders", symbol))
        return self.open_rows

    def order_history(self, mode, symbol=None, limit=100):
        self.events.append(("history", mode, symbol, limit))
        return self.history_rows

    def place_order(self, order_intent):
        self.events.append(("place_order", order_intent.client_order_id))
        if isinstance(self.place_result, Exception):
            raise self.place_result
        return self.place_result

    def place_tp_sl(self, **kwargs):
        self.events.append(("place_tp_sl", kwargs["plan_type"], kwargs["client_algo_id"]))
        return {"success": True, "orderId": len(self.events)}

    def algo_orders(self, symbol):
        self.events.append(("algo_orders", symbol))
        client_ids = [event[2] for event in self.events if event[0] == "place_tp_sl"]
        return [{"clientAlgoId": value, "orderId": index} for index, value in enumerate(client_ids, 1)]

    def cancel_algo_order(self, order_id):
        self.events.append(("cancel_algo", order_id))
        return {"success": True}


def test_precheck_blocks_existing_position() -> None:
    gateway = FakeGateway()
    gateway.positions_rows = [{"symbol": "BTCSUSDT", "size": "1"}]
    with pytest.raises(SafetyError, match="existing position"):
        TradingService(gateway).submit_order(intent())  # type: ignore[arg-type]
    assert not any(event[0] == "place_order" for event in gateway.events)


def test_precheck_retries_transient_position_read_without_duplicate_submission() -> None:
    gateway = FakeGateway()
    calls = 0
    delays: list[float] = []

    def flaky_positions(mode, symbol=None):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ccxt.RequestTimeout("precheck timeout")
        return []

    gateway.positions = flaky_positions

    result = TradingService(gateway, sleep=delays.append).submit_order(intent())  # type: ignore[arg-type]

    assert result["status"] == "submitted"
    assert calls >= 2
    assert delays == [0.25]
    assert len([event for event in gateway.events if event[0] == "place_order"]) == 1


def test_successful_submit_verifies_history_and_positions() -> None:
    gateway = FakeGateway()
    gateway.history_rows = [None, "malformed", {"clientOrderId": "client-1", "status": "FILLED"}]
    result = TradingService(gateway).submit_order(intent())  # type: ignore[arg-type]
    assert result["status"] == "submitted"
    assert result["verification"]["order_found"] is True


def test_successful_submit_is_not_mislabeled_when_immediate_verification_times_out() -> None:
    gateway = FakeGateway()
    position_calls = 0
    delays: list[float] = []

    def flaky_positions(mode, symbol=None):
        nonlocal position_calls
        position_calls += 1
        if position_calls == 2:
            raise ccxt.RequestTimeout("verification timeout")
        return []

    gateway.positions = flaky_positions

    result = TradingService(gateway, sleep=delays.append).submit_order(intent())  # type: ignore[arg-type]

    assert result["status"] == "submitted"
    assert result["result"]["success"] is True
    assert result["verification"]["order_found"] is False
    assert position_calls == 3
    assert delays == [0.25]
    assert len([event for event in gateway.events if event[0] == "place_order"]) == 1


def test_network_error_recovers_by_client_order_id_without_retry() -> None:
    gateway = FakeGateway()
    gateway.place_result = ccxt.NetworkError("timeout")
    gateway.history_rows = [{"clientOrderId": "client-1", "status": "NEW"}]
    result = TradingService(gateway).submit_order(intent())  # type: ignore[arg-type]
    assert result["status"] == "recovered_after_submit_error"
    assert len([event for event in gateway.events if event[0] == "place_order"]) == 1


def test_submit_timeout_recovers_when_order_visibility_is_delayed_without_resubmitting() -> None:
    gateway = FakeGateway()
    gateway.place_result = ccxt.RequestTimeout("timeout")
    history_calls = 0
    delays: list[float] = []

    def delayed_history(mode, symbol=None, limit=100):
        nonlocal history_calls
        history_calls += 1
        if history_calls < 3:
            return []
        return [{"clientOrderId": "client-1", "status": "NEW"}]

    gateway.order_history = delayed_history

    result = TradingService(gateway, sleep=delays.append).submit_order(intent())  # type: ignore[arg-type]

    assert result["status"] == "recovered_after_submit_error"
    assert history_calls == 3
    assert delays == [0.25, 0.5]
    assert len([event for event in gateway.events if event[0] == "place_order"]) == 1


def test_unknown_network_outcome_never_retries() -> None:
    gateway = FakeGateway()
    gateway.place_result = ccxt.RequestTimeout("timeout")
    with pytest.raises(SubmissionUncertainError, match="inspect orders before retrying"):
        TradingService(gateway, sleep=lambda _: None).submit_order(intent())  # type: ignore[arg-type]
    assert len([event for event in gateway.events if event[0] == "place_order"]) == 1


def test_bracket_places_and_verifies_stop_before_take_profit() -> None:
    gateway = FakeGateway()
    result = TradingService(gateway).place_bracket(  # type: ignore[arg-type]
        symbol="BTC",
        position_side="LONG",
        take_profit="63000",
        stop_loss="58500",
        quantity="0",
        trigger_price_type="MARK_PRICE",
        client_prefix="bracket-1",
    )
    sequence = [event[:2] for event in gateway.events]
    assert sequence[:3] == [("place_tp_sl", "STOP_LOSS"), ("algo_orders", "BTC"), ("place_tp_sl", "TAKE_PROFIT")]
    assert result["status"] == "submitted"


def test_replace_stop_verifies_new_before_canceling_old() -> None:
    gateway = FakeGateway()
    result = TradingService(gateway).replace_stop(  # type: ignore[arg-type]
        symbol="BTC",
        old_order_id="old",
        trigger_price="59000",
        position_side="LONG",
        quantity="0",
        trigger_price_type="MARK_PRICE",
        client_algo_id="new-sl",
    )
    assert [event[0] for event in gateway.events[:3]] == ["place_tp_sl", "algo_orders", "cancel_algo"]
    assert result["status"] == "replaced"
