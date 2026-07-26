from __future__ import annotations

import pytest

from weex_cli.core.errors import ValidationError
from weex_cli.core.models import OrderIntent, decimal_text, decimal_value
from weex_cli.core.symbols import base_asset, ccxt_swap_symbol, demo_symbol_id, live_symbol_id


@pytest.mark.parametrize("value", ["BTC", "BTCUSDT", "BTCSUSDT", "BTC/USDT:USDT", "btc-usdt"])
def test_symbol_variants_map_to_btc(value: str) -> None:
    assert base_asset(value) == "BTC"
    assert live_symbol_id(value) == "BTCUSDT"
    assert demo_symbol_id(value) == "BTCSUSDT"
    assert ccxt_swap_symbol(value) == "BTC/USDT:USDT"


def test_demo_limit_payload_uses_post_only_and_attached_protection() -> None:
    intent = OrderIntent.create(
        mode="demo",
        symbol="BTC",
        side="buy",
        position_side="long",
        order_type="limit",
        quantity="0.001",
        price="60000",
        take_profit="63000",
        stop_loss="58500",
        client_order_id="demo-1",
    )
    assert intent.demo_payload() == {
        "symbol": "BTCSUSDT",
        "side": "BUY",
        "positionSide": "LONG",
        "type": "LIMIT",
        "quantity": "0.001",
        "newClientOrderId": "demo-1",
        "price": "60000",
        "timeInForce": "POST_ONLY",
        "tpTriggerPrice": "63000",
        "TpWorkingType": "CONTRACT_PRICE",
        "slTriggerPrice": "58500",
        "SlWorkingType": "MARK_PRICE",
    }


def test_live_order_maps_reduce_only_and_trigger_types() -> None:
    intent = OrderIntent.create(
        mode="live",
        symbol="ETHUSDT",
        side="sell",
        position_side="long",
        order_type="market",
        quantity="0.5",
        reduce_only=True,
        stop_loss="3500",
        sl_trigger_type="CONTRACT_PRICE",
        client_order_id="live-1",
    )
    symbol, order_type, side, quantity, price, params = intent.live_order()
    assert (symbol, order_type, side, quantity, price) == ("ETH/USDT:USDT", "market", "sell", 0.5, None)
    assert params["reduceOnly"] is True
    assert params["positionSide"] == "LONG"
    assert params["stopLoss"] == {"triggerPrice": 3500.0, "triggerPriceType": "last"}


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"order_type": "stop"}, "order_type"),
        ({"side": "hold"}, "side"),
        ({"position_side": "flat"}, "position_side"),
        ({"quantity": "0"}, "quantity"),
        ({"order_type": "limit", "price": None}, "price"),
        ({"order_type": "market", "time_in_force": "GTC"}, "time_in_force"),
    ],
)
def test_invalid_order_intents_are_rejected(kwargs: dict[str, object], message: str) -> None:
    values: dict[str, object] = {
        "mode": "demo",
        "symbol": "BTC",
        "side": "buy",
        "position_side": "long",
        "order_type": "market",
        "quantity": "1",
        "client_order_id": "test",
    }
    values.update(kwargs)
    with pytest.raises(ValidationError, match=message):
        OrderIntent.create(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "side, position_side, reduce_only, message",
    [
        ("sell", "long", False, "open long"),
        ("buy", "short", False, "open short"),
        ("buy", "long", True, "reduce long"),
        ("sell", "short", True, "reduce short"),
    ],
)
def test_order_direction_must_match_position_action(
    side: str, position_side: str, reduce_only: bool, message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        OrderIntent.create(
            mode="live",
            symbol="BTC",
            side=side,
            position_side=position_side,
            order_type="market",
            quantity="1",
            reduce_only=reduce_only,
        )


@pytest.mark.parametrize("client_order_id", ["bad key", "bad+key", "x" * 37])
def test_client_order_id_rejects_unsupported_characters(client_order_id: str) -> None:
    with pytest.raises(ValidationError, match="client_order_id"):
        OrderIntent.create(
            mode="demo",
            symbol="BTC",
            side="buy",
            position_side="long",
            order_type="market",
            quantity="1",
            client_order_id=client_order_id,
        )


@pytest.mark.parametrize(
    "side, position_side, take_profit, stop_loss, message",
    [
        ("buy", "long", "60000", "59000", "long take_profit"),
        ("buy", "long", "61000", "60000", "long stop_loss"),
        ("sell", "short", "60000", "61000", "short take_profit"),
        ("sell", "short", "59000", "60000", "short stop_loss"),
    ],
)
def test_limit_entry_protection_must_be_on_the_correct_side(
    side: str, position_side: str, take_profit: str, stop_loss: str, message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        OrderIntent.create(
            mode="demo",
            symbol="BTC",
            side=side,
            position_side=position_side,
            order_type="limit",
            quantity="1",
            price="60000",
            take_profit=take_profit,
            stop_loss=stop_loss,
        )


def test_decimal_helpers_avoid_exponent_notation() -> None:
    assert decimal_text(decimal_value("1.2300", name="value")) == "1.23"
    assert decimal_value("0", name="value", allow_zero=True) == 0
