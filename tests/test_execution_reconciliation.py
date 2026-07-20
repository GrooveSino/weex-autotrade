from __future__ import annotations

from decimal import Decimal

from weex_cli.execution_reconciliation import LegFillRequest, LiveLegFillReconciler


class Reporter:
    def __init__(self, payloads: list[dict]) -> None:
        self.payloads = payloads
        self.calls = 0

    def report(self, **kwargs) -> dict:
        payload = self.payloads[min(self.calls, len(self.payloads) - 1)]
        self.calls += 1
        return payload


class FlakyReporter(Reporter):
    def report(self, **kwargs) -> dict:
        if self.calls == 0:
            self.calls += 1
            raise ConnectionError("temporary read failure")
        return super().report(**kwargs)


class ExactOrderReporter(Reporter):
    def __init__(self, payloads: list[dict]) -> None:
        super().__init__(payloads)
        self.order_calls: list[dict] = []

    def report_order_ids(self, **kwargs) -> dict:
        self.order_calls.append(kwargs)
        return super().report(**kwargs)


def request() -> LegFillRequest:
    return LegFillRequest(
        sequence=1,
        symbol="BTC",
        action="open",
        expected_quantity=Decimal("0.0003"),
        tolerance_quantity=Decimal("0.00005"),
        order_ids=("wanted", "canceled-without-fill"),
        started_at_ms=1_000,
        ended_at_ms=2_000,
    )


def trade(*, order_id: str = "wanted", maker: bool | None = True) -> dict:
    return {
        "trade_id": "fill-1",
        "order_id": order_id,
        "symbol": "BTCUSDT",
        "position_action": "open",
        "quantity": "0.0003",
        "quote_quantity": "19.2",
        "maker": maker,
        "commission": "0.00384",
        "commission_asset": "USDT",
        "realized_pnl": "0",
    }


def test_reconciler_filters_by_submitted_order_and_uses_authoritative_fill_fields() -> None:
    reporter = Reporter([{"complete": True, "warnings": [], "trades": [trade(), trade(order_id="unrelated")]}])
    target = LiveLegFillReconciler(
        object(),
        reporter=reporter,
        now_ms=lambda: 2_000,
        sleep=lambda _: None,
    )

    result = target.reconcile(request())

    assert result.verified is True
    assert result.fill_count == 1
    assert result.order_count == 1
    assert result.quote_volume == Decimal("19.2")
    assert result.commission_by_asset == {"USDT": Decimal("0.00384")}


def test_reconciler_prefers_exact_order_id_reporting_when_available() -> None:
    reporter = ExactOrderReporter([{"complete": True, "warnings": [], "trades": [trade()]}])
    result = LiveLegFillReconciler(
        object(),
        reporter=reporter,
        now_ms=lambda: 2_000,
        sleep=lambda _: None,
    ).reconcile(request())

    assert result.verified is True
    assert reporter.order_calls == [
        {
            "symbol": "BTC",
            "order_ids": ("wanted", "canceled-without-fill"),
            "start_time": 0,
            "end_time": 2_000,
        }
    ]


def test_reconciler_rejects_taker_or_unknown_liquidity() -> None:
    for maker, expected in ((False, "taker_fill_detected"), (None, "unknown_liquidity")):
        reporter = Reporter([{"complete": True, "warnings": [], "trades": [trade(maker=maker)]}])
        result = LiveLegFillReconciler(object(), reporter=reporter, now_ms=lambda: 2_000).reconcile(request())

        assert result.verified is False
        assert result.status == expected
        assert result.maker_only is False


def test_reconciler_retries_read_only_visibility_without_claiming_a_fill() -> None:
    reporter = Reporter([{"complete": True, "warnings": [], "trades": []}])
    delays: list[float] = []
    result = LiveLegFillReconciler(
        object(),
        reporter=reporter,
        attempts=3,
        now_ms=lambda: 2_000,
        sleep=delays.append,
    ).reconcile(request())

    assert result.status == "fills_not_visible"
    assert result.verified is False
    assert reporter.calls == 3
    assert delays == [1.0, 2.0]


def test_reconciler_waits_for_a_delayed_authoritative_fill() -> None:
    reporter = Reporter(
        [
            {"complete": True, "warnings": [], "trades": []},
            {"complete": True, "warnings": [], "trades": []},
            {"complete": True, "warnings": [], "trades": []},
            {"complete": True, "warnings": [], "trades": [trade()]},
        ]
    )
    delays: list[float] = []

    result = LiveLegFillReconciler(
        object(),
        reporter=reporter,
        now_ms=lambda: 2_000,
        sleep=delays.append,
    ).reconcile(request())

    assert result.verified is True
    assert reporter.calls == 4
    assert delays == [1.0, 2.0, 3.0]


def test_reconciler_retries_only_the_read_after_transport_failure() -> None:
    reporter = FlakyReporter([{"complete": True, "warnings": [], "trades": [trade()]}])
    delays: list[float] = []
    result = LiveLegFillReconciler(
        object(),
        reporter=reporter,
        attempts=2,
        now_ms=lambda: 2_000,
        sleep=delays.append,
    ).reconcile(request())

    assert result.verified is True
    assert reporter.calls == 2
    assert delays == [1.0]
