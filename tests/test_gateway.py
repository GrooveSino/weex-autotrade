from __future__ import annotations

import pytest

from tests.fakes import FakeExchange
from weex_cli.config import Settings
from weex_cli.errors import UnsupportedModeError, ValidationError
from weex_cli.gateway import WeexGateway, ensure_live
from weex_cli.models import OrderIntent


@pytest.fixture
def gateway() -> tuple[WeexGateway, FakeExchange]:
    fake = FakeExchange()
    return WeexGateway(Settings.load(environ={}), fake), fake


def test_public_market_calls_use_ccxt_swap_symbol(gateway) -> None:
    target, fake = gateway
    assert target.ticker("BTC")["last"] == 100.0
    target.order_book("BTC", 5)
    assert fake.calls == [
        ("fetch_ticker", "BTC/USDT:USDT"),
        ("fetch_order_book", "BTC/USDT:USDT", 5),
    ]


def test_demo_routes_and_symbol_filter(gateway) -> None:
    target, fake = gateway
    fake.responses[("GET", "capi/v3/sim/balance")] = [{"asset": "SUSDT"}]
    fake.responses[("GET", "capi/v3/sim/position/allPosition")] = [
        {"symbol": "BTCSUSDT", "size": "1"},
        {"symbol": "ETHSUSDT", "size": "2"},
    ]
    assert target.balance("demo") == [{"asset": "SUSDT"}]
    assert target.positions("demo", "BTC") == [{"symbol": "BTCSUSDT", "size": "1"}]
    target.order_history("demo", "BTC", 20, 1, 2)
    assert fake.calls[-1][-1] == {"limit": 20, "symbol": "BTCSUSDT", "startTime": 1, "endTime": 2}


def test_live_account_and_order_management(gateway) -> None:
    target, fake = gateway
    assert target.balance("live") == {"USDT": {"free": 10}}
    assert target.positions("live", "BTC") == []
    target.open_orders("BTC", trigger=True)
    target.cancel_order("BTC", "1", trigger=True)
    target.cancel_all_orders("BTC")
    result = target.configure_position("BTC", 10, "isolated")
    assert result["leverage"] == {"leverage": 10}
    fake.position_rows = [{"id": "long-position", "side": "long", "contracts": 1}]
    target.close_position("BTC", "long")
    target.close_all_positions()
    assert any(call[0] == "set_margin_mode" for call in fake.calls)
    close_calls = [call for call in fake.calls if call[:2] == ("request", "capi/v3/closePositions")]
    assert close_calls[0][-1] == {"symbol": "BTCUSDT", "positionId": "long-position"}
    assert close_calls[1][-1] == {}


def test_close_position_rejects_missing_or_ambiguous_side(gateway) -> None:
    target, fake = gateway
    with pytest.raises(ValidationError, match="no active short"):
        target.close_position("BTC", "short")
    fake.position_rows = [
        {"id": "long-1", "side": "long", "contracts": 1},
        {"id": "long-2", "side": "long", "contracts": 2},
    ]
    with pytest.raises(ValidationError, match="multiple active long"):
        target.close_position("BTC", "long")


def test_place_demo_and_live_orders(gateway) -> None:
    target, fake = gateway
    demo = OrderIntent.create(
        mode="demo",
        symbol="BTC",
        side="buy",
        position_side="long",
        order_type="limit",
        quantity="0.001",
        price="60000",
        client_order_id="demo-1",
    )
    live = OrderIntent.create(
        mode="live",
        symbol="BTC",
        side="sell",
        position_side="short",
        order_type="limit",
        quantity="0.001",
        price="70000",
        client_order_id="live-1",
    )
    assert target.place_order(demo)["path"] == "capi/v3/sim/order"
    assert target.place_order(live)["id"] == "live-order"
    assert fake.calls[-1][0] == "create_order"


def test_risk_endpoints_use_documented_payloads(gateway) -> None:
    target, fake = gateway
    target.place_tp_sl(
        symbol="BTC", plan_type="STOP_LOSS", trigger_price="59000", position_side="LONG", client_algo_id="sl-1"
    )
    assert fake.calls[-1][-1]["symbol"] == "BTCUSDT"
    target.modify_tp_sl(order_id="9", trigger_price="59500")
    assert fake.calls[-1][-1] == {
        "orderId": "9",
        "triggerPrice": "59500",
        "executePrice": "0",
        "triggerPriceType": "MARK_PRICE",
    }
    target.algo_orders("BTC")
    target.cancel_algo_order("9")
    assert fake.calls[-1][-1] == {"orderId": "9"}


def test_demo_only_operation_guard() -> None:
    with pytest.raises(UnsupportedModeError):
        ensure_live("demo", "cancel")
    ensure_live("live", "cancel")
