from __future__ import annotations

from typing import Any

import pytest

import weex_cli.trade_reporting as trade_reporting
from weex_cli.core.errors import ValidationError
from weex_cli.trade_reporting import TradeReportService, current_timestamp_ms, parse_timestamp


class FakeGateway:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls: list[dict[str, Any]] = []

    def trade_rows(self, mode, symbol, **kwargs):
        self.calls.append({"mode": mode, "symbol": symbol, **kwargs})
        if mode == "demo" and kwargs.get("page", 0) > 0:
            return []
        return self.rows

    def trade_rows_by_order_id(self, symbol, order_id, **kwargs):
        self.calls.append({"symbol": symbol, "order_id": order_id, **kwargs})
        return [row for row in self.rows if str(row.get("orderId") or "") == order_id]


def test_demo_report_counts_opening_and_closing_volume() -> None:
    gateway = FakeGateway(
        [
            {
                "orderId": "open-1",
                "symbol": "BTCSUSDT",
                "side": "BUY",
                "positionSide": "LONG",
                "status": "FILLED",
                "executedQty": "0.1",
                "avgPrice": "100",
                "cumQuote": "10",
                "timeInForce": "POST_ONLY",
                "updateTime": 1000,
            },
            {
                "orderId": "close-1",
                "symbol": "BTCUSDT",
                "side": "SELL",
                "positionSide": "LONG",
                "status": "FILLED",
                "executedQty": "0.1",
                "avgPrice": "110",
                "cumQuote": "11",
                "timeInForce": "POST_ONLY",
                "updateTime": 2000,
            },
            {
                "orderId": "other-symbol",
                "symbol": "ETHSUSDT",
                "side": "BUY",
                "positionSide": "LONG",
                "status": "FILLED",
                "executedQty": "1",
                "avgPrice": "100",
                "cumQuote": "100",
                "timeInForce": "POST_ONLY",
                "updateTime": 2250,
            },
            {"orderId": "unfilled", "executedQty": "0", "updateTime": 2500},
        ]
    )

    report = TradeReportService(gateway).report(mode="demo", symbol="BTC", start_time=0, end_time=3000)  # type: ignore[arg-type]

    assert report["source"] == "demo_order_history"
    assert report["granularity"] == "order"
    assert report["complete"] is True
    assert report["summary"] == {
        "trade_count": 2,
        "order_count": 2,
        "quote_asset": "SUSDT",
        "total_quote_volume": "21",
        "opening_quote_volume": "10",
        "closing_quote_volume": "11",
        "unknown_action_quote_volume": "0",
        "buy_quote_volume": "10",
        "sell_quote_volume": "11",
        "maker_quote_volume": "21",
        "taker_quote_volume": "0",
        "unknown_liquidity_quote_volume": "0",
        "maker_count": 2,
        "taker_count": 0,
        "unknown_liquidity_count": 0,
        "base_quantity_by_symbol": {"BTCSUSDT": "0.1", "BTCUSDT": "0.1"},
        "commission_by_asset": {},
        "realized_pnl": "0",
    }


def test_live_report_uses_fill_fields_and_fee_assets() -> None:
    now = current_timestamp_ms()
    gateway = FakeGateway(
        [
            {
                "id": "fill-1",
                "orderId": "order-1",
                "symbol": "BTCUSDT",
                "side": "BUY",
                "positionSide": "LONG",
                "price": "100",
                "qty": "2",
                "quoteQty": "200",
                "maker": True,
                "commission": "0.02",
                "commissionAsset": "USDT",
                "realizedPnl": "0",
                "time": now - 1000,
            },
            {
                "id": "fill-2",
                "orderId": "order-2",
                "symbol": "BTCUSDT",
                "side": "SELL",
                "positionSide": "LONG",
                "price": "105",
                "qty": "2",
                "quoteQty": "210",
                "maker": False,
                "commission": "0.03",
                "commissionAsset": "USDT",
                "realizedPnl": "10",
                "time": now,
            },
        ]
    )

    report = TradeReportService(gateway).report(mode="live", symbol="BTC", start_time=now - 2000, end_time=now)  # type: ignore[arg-type]
    summary = report["summary"]

    assert report["granularity"] == "fill"
    assert summary["total_quote_volume"] == "410"
    assert summary["opening_quote_volume"] == "200"
    assert summary["closing_quote_volume"] == "210"
    assert summary["maker_quote_volume"] == "200"
    assert summary["taker_quote_volume"] == "210"
    assert summary["commission_by_asset"] == {"USDT": "0.05"}
    assert summary["realized_pnl"] == "10"


def test_live_order_id_report_uses_exact_endpoint_and_filters_rows() -> None:
    now = current_timestamp_ms()
    gateway = FakeGateway(
        [
            {
                "id": "fill-1",
                "orderId": "wanted",
                "symbol": "BTCUSDT",
                "side": "BUY",
                "positionSide": "LONG",
                "price": "100",
                "qty": "1",
                "quoteQty": "100",
                "maker": True,
                "time": now,
            },
            {
                "id": "fill-2",
                "orderId": "other",
                "symbol": "BTCUSDT",
                "qty": "1",
                "quoteQty": "200",
                "maker": True,
                "time": now,
            },
        ]
    )

    report = TradeReportService(gateway).report_order_ids(
        symbol="BTC",
        order_ids=("wanted",),
        start_time=now - 1,
        end_time=now,
    )

    assert report["complete"] is True
    assert report["summary"]["total_quote_volume"] == "100"
    assert gateway.calls == [
        {"symbol": "BTC", "order_id": "wanted", "start_time": now - 1, "end_time": now, "limit": 100}
    ]


def test_demo_report_paginates_until_short_page(monkeypatch) -> None:
    monkeypatch.setattr(trade_reporting, "DEMO_LIMIT", 2)

    class PagingGateway(FakeGateway):
        def trade_rows(self, mode, symbol, **kwargs):
            self.calls.append({"mode": mode, "symbol": symbol, **kwargs})
            page = kwargs.get("page", 0)
            if page == 0:
                return [
                    {"orderId": "1", "executedQty": "1", "avgPrice": "1", "updateTime": 1},
                    {"orderId": "2", "executedQty": "1", "avgPrice": "1", "updateTime": 2},
                ]
            if page == 1:
                return [{"orderId": "3", "executedQty": "1", "avgPrice": "1", "updateTime": 3}]
            return []

    gateway = PagingGateway([])
    report = TradeReportService(gateway).report(mode="demo", symbol=None, start_time=0, end_time=10)  # type: ignore[arg-type]

    assert report["summary"]["trade_count"] == 3
    assert [call["page"] for call in gateway.calls] == [0, 1]


def test_parse_timestamp_requires_timezone_and_supports_seconds() -> None:
    assert parse_timestamp("2026-07-17T00:00:00+08:00", name="start") == 1784217600000
    assert parse_timestamp("1784217600", name="start") == 1784217600000
    with pytest.raises(ValidationError, match="timezone"):
        parse_timestamp("2026-07-17T00:00:00", name="start")
    with pytest.raises(ValidationError, match="ISO-8601"):
        parse_timestamp("178421760000", name="start")


def test_live_report_marks_saturated_request_budget_incomplete(monkeypatch) -> None:
    monkeypatch.setattr(trade_reporting, "LIVE_LIMIT", 2)
    monkeypatch.setattr(trade_reporting, "MAX_REQUESTS", 1)
    now = current_timestamp_ms()
    gateway = FakeGateway(
        [
            {"id": "1", "qty": "1", "price": "1", "time": now - 1},
            {"id": "2", "qty": "1", "price": "1", "time": now},
        ]
    )

    report = TradeReportService(gateway).report(mode="live", symbol="BTC", start_time=now - 1, end_time=now)  # type: ignore[arg-type]

    assert report["complete"] is False
    assert report["summary"]["total_quote_volume"] == "0"
    assert "totals are incomplete" in report["warnings"][0]
