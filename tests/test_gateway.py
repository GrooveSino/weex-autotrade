from __future__ import annotations

from decimal import Decimal

import ccxt
import pytest

from tests.fakes import FakeExchange
from weex_cli.core.config import Credentials, Settings
from weex_cli.core.errors import UnsupportedModeError, ValidationError
from weex_cli.core.models import OrderIntent
from weex_cli.exchange.rest.gateway import WeexGateway, build_client, ensure_live


@pytest.fixture
def gateway() -> tuple[WeexGateway, FakeExchange]:
    fake = FakeExchange()
    return WeexGateway(Settings.load(environ={}), fake), fake


def test_public_market_calls_use_ccxt_swap_symbol(gateway) -> None:
    target, fake = gateway
    assert target.ticker("BTC")["last"] == 100.0
    target.order_book("BTC", 5)
    assert target.amount_to_precision("BTC", Decimal("1.23456")) == Decimal("1.2346")
    assert fake.calls == [
        ("fetch_ticker", "BTC/USDT:USDT"),
        ("fetch_order_book", "BTC/USDT:USDT", 5),
        ("amount_to_precision", "BTC/USDT:USDT", "1.23456"),
    ]


def test_fork_creates_an_uninitialized_independent_client_boundary() -> None:
    settings = Settings(credentials=Credentials("key", "secret", "passphrase"))
    original = WeexGateway(settings, FakeExchange(), proxy_url="http://user:pass@proxy.example:8080")

    first = original.fork()
    second = original.fork()

    assert first is not second
    assert first is not original
    assert first.settings is settings
    assert first._client is None
    assert second._client is None
    assert first._proxy_url == "http://user:pass@proxy.example:8080"


def test_demo_routes_and_symbol_filter(gateway) -> None:
    target, fake = gateway
    fake.responses[("GET", "capi/v3/sim/balance")] = [{"asset": "SUSDT"}]
    fake.responses[("GET", "capi/v3/sim/position/allPosition")] = [
        {"symbol": "BTCSUSDT", "size": "1"},
        {"symbol": "ETHSUSDT", "size": "2"},
    ]
    fake.responses[("GET", "capi/v3/sim/order/history")] = [
        {"symbol": "BTCSUSDT", "orderId": "btc-demo"},
        {"symbol": "BTCUSDT", "orderId": "btc-live-id"},
        {"symbol": "ETHSUSDT", "orderId": "eth-demo"},
    ]
    assert target.balance("demo") == [{"asset": "SUSDT"}]
    assert target.positions("demo", "BTC") == [{"symbol": "BTCSUSDT", "size": "1"}]
    assert target.order_history("demo", "BTC", 20, 1, 2) == [
        {"symbol": "BTCSUSDT", "orderId": "btc-demo"},
        {"symbol": "BTCUSDT", "orderId": "btc-live-id"},
    ]
    assert fake.calls[-1][-1] == {"limit": 20, "startTime": 1, "endTime": 2}


def test_raw_readonly_account_routes_use_documented_paths(gateway) -> None:
    target, fake = gateway
    fake.responses[("GET", "capi/v3/account/balance")] = [{"asset": "USDT"}]
    fake.responses[("GET", "capi/v3/account/position/allPosition")] = [{"symbol": "BTCUSDT"}]

    assert target.account_balance_rows("live") == [{"asset": "USDT"}]
    assert target.all_position_rows("live") == [{"symbol": "BTCUSDT"}]
    assert fake.calls[-2][1:4] == ("capi/v3/account/balance", "contractPrivate", "GET")
    assert fake.calls[-1][1:4] == ("capi/v3/account/position/allPosition", "contractPrivate", "GET")


@pytest.mark.parametrize(
    ("proxy_url", "proxy_key"),
    [
        ("http://user:pass@proxy.example:8080", "httpsProxy"),
        ("socks5://user:pass@proxy.example:1080", "socksProxy"),
    ],
)
def test_explicit_account_proxy_disables_environment_proxy_inheritance(monkeypatch, proxy_url, proxy_key) -> None:
    import ccxt

    captured: dict[str, object] = {}

    def fake_weex(config):
        captured.update(config)
        return object()

    monkeypatch.setattr(ccxt, "weex", fake_weex)
    settings = Settings(credentials=Credentials("key", "secret", "passphrase"))

    build_client(settings, require_private=True, proxy_url=proxy_url)

    assert captured[proxy_key] == proxy_url
    assert captured["requests_trust_env"] is False
    assert {"apiKey", "secret", "password"} <= captured.keys()


def test_unsupported_explicit_proxy_scheme_is_rejected_before_client_creation() -> None:
    settings = Settings(credentials=Credentials("key", "secret", "passphrase"))
    with pytest.raises(ValidationError, match="proxy URL"):
        build_client(settings, require_private=True, proxy_url="ftp://proxy.example:21")


def test_live_account_and_order_management(gateway) -> None:
    target, fake = gateway
    assert target.balance("live") == {"USDT": {"free": 10}}
    assert target.positions("live", "BTC") == []
    target.open_orders("BTC", trigger=True, mode="live")
    target.cancel_order("BTC", "1", trigger=True, mode="live")
    target.cancel_all_orders("BTC", mode="live")
    result = target.configure_position("BTC", 10, "isolated")
    assert result["leverage"] == {"leverage": 10}
    assert target.leverage("BTC") == {
        "marginMode": "isolated",
        "longLeverage": 10,
        "shortLeverage": 10,
    }
    fake.position_rows = [{"id": "long-position", "side": "long", "contracts": 1}]
    target.close_position("BTC", "long")
    target.close_all_positions()
    assert any(call[0] == "set_margin_mode" for call in fake.calls)
    close_calls = [call for call in fake.calls if call[:2] == ("request", "capi/v3/closePositions")]
    assert close_calls[0][-1] == {"symbol": "BTCUSDT", "positionId": "long-position"}
    assert close_calls[1][-1] == {}


def test_close_position_id_uses_the_official_numeric_position_boundary(gateway) -> None:
    target, fake = gateway

    target.close_position_id("BTC", "689987235755328154")

    close_call = [call for call in fake.calls if call[:2] == ("request", "capi/v3/closePositions")][-1]
    assert close_call[-1] == {"symbol": "BTCUSDT", "positionId": 689987235755328154}
    with pytest.raises(ValidationError, match="position ID"):
        target.close_position_id("BTC", "position-7")


def test_weex_code_200_mutation_envelope_is_treated_as_success() -> None:
    class SuccessEnvelopeExchange(FakeExchange):
        def set_margin_mode(self, mode, symbol):
            self.calls.append(("set_margin_mode", mode, symbol))
            raise ccxt.ExchangeError('weex {"msg":"success","requestTime":1,"code":"200"}')

        def set_leverage(self, leverage, symbol, params):
            self.calls.append(("set_leverage", leverage, symbol, params))
            raise ccxt.ExchangeError('weex {"msg":"success","requestTime":2,"code":"200"}')

    fake = SuccessEnvelopeExchange()
    target = WeexGateway(Settings.load(environ={}), fake)

    result = target.configure_position("BTC", 5, "isolated")

    assert result == {
        "margin_mode": {"status": "accepted", "exchange_code": "200"},
        "leverage": {"status": "accepted", "exchange_code": "200"},
    }
    assert [call[0] for call in fake.calls] == ["set_margin_mode", "set_leverage"]


def test_non_success_mutation_envelope_remains_an_error() -> None:
    class FailedExchange(FakeExchange):
        def set_leverage(self, leverage, symbol, params):
            raise ccxt.ExchangeError('weex {"msg":"invalid leverage","code":"400"}')

    target = WeexGateway(Settings.load(environ={}), FailedExchange())

    with pytest.raises(ccxt.ExchangeError, match="invalid leverage"):
        target.configure_leverage("BTC", 5, "isolated")


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


def test_trade_rows_use_live_symbols_and_mode_specific_endpoints(gateway) -> None:
    target, fake = gateway
    target.trade_rows("demo", "BTC", start_time=1, end_time=2, limit=1000, page=3)
    assert fake.calls[-1] == (
        "request",
        "capi/v3/sim/order/history",
        "contractPrivate",
        "GET",
        {"startTime": 1, "endTime": 2, "limit": 1000, "page": 3},
    )
    target.trade_rows("live", "BTC", start_time=1, end_time=2, limit=100)
    assert fake.calls[-1] == (
        "request",
        "capi/v3/userTrades",
        "contractPrivate",
        "GET",
        {"startTime": 1, "endTime": 2, "limit": 100, "symbol": "BTCUSDT"},
    )
    target.trade_rows_by_order_id("BTC", "123", start_time=1, end_time=2)
    assert fake.calls[-1] == (
        "request",
        "capi/v3/userTrades",
        "contractPrivate",
        "GET",
        {"symbol": "BTCUSDT", "orderId": "123", "startTime": 1, "endTime": 2, "limit": 100},
    )


def test_demo_only_operation_guard() -> None:
    with pytest.raises(UnsupportedModeError):
        ensure_live("demo", "cancel")
    ensure_live("live", "cancel")
