from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import ccxt

from weex_cli.beta_volume import accounting_summary
from weex_cli.execution.dust_position_close import close_dust_position_once
from weex_cli.execution.reconciliation import LegFillReport


class IntentStore:
    def __init__(self) -> None:
        self.claims: set[str] = set()

    def claim_market_close_intent(self, plan, key: str, *, created_at_ms: int) -> bool:  # noqa: ANN001
        del plan, created_at_ms
        if key in self.claims:
            return False
        self.claims.add(key)
        return True


class Reconciler:
    def __init__(self, *, visible: bool = True) -> None:
        self.visible = visible

    def reconcile(self, request) -> LegFillReport:  # noqa: ANN001
        if not self.visible:
            raise ccxt.RequestTimeout("fills are not visible yet")
        return LegFillReport(
            status="verified",
            source_complete=True,
            fill_count=1,
            order_count=1,
            executed_quantity=request.expected_quantity,
            quote_volume=request.expected_quantity * Decimal("5"),
            maker_only=False,
            maker_count=0,
            taker_count=1,
            unknown_liquidity_count=0,
            commission_by_asset={"USDT": Decimal("0.01")},
            realized_pnl=Decimal(0),
        )


class Gateway:
    def __init__(
        self,
        *,
        quantity: str = "1",
        quote: str = "5",
        minimum: str = "0.1",
        close_outcome: str = "success",
    ) -> None:
        self.quantity = Decimal(quantity)
        self.quote = Decimal(quote)
        self.minimum = Decimal(minimum)
        self.close_outcome = close_outcome
        self.close_calls: list[tuple[str, str]] = []
        self.regular_orders: list[dict[str, str]] = []
        self.trigger_orders: list[dict[str, str]] = []

    def positions(self, mode: str, symbol: str) -> list[dict[str, object]]:
        assert mode == "live"
        if self.close_outcome == "verification_timeout" and self.close_calls:
            raise ccxt.RequestTimeout("position read timed out")
        if self.quantity <= 0:
            return []
        return [
            {
                "id": "position-7",
                "side": "long",
                "contracts": str(self.quantity),
                "notional": str(self.quote),
                "symbol": symbol,
            }
        ]

    def minimum_amount(self, symbol: str) -> Decimal:
        assert symbol == "BTC"
        return self.minimum

    def open_orders(self, symbol: str, *, mode: str) -> list[dict[str, str]]:
        assert symbol == "BTC" and mode == "live"
        return list(self.regular_orders)

    def algo_orders(self, symbol: str) -> list[dict[str, str]]:
        assert symbol == "BTC"
        return list(self.trigger_orders)

    def close_position_id(self, symbol: str, position_id: str):  # noqa: ANN201
        self.close_calls.append((symbol, position_id))
        if self.close_outcome == "rejected":
            return [{"success": False, "errorMessage": "rejected"}]
        self.quantity = Decimal(0)
        if self.close_outcome == "transport_after_apply":
            raise ccxt.RequestTimeout("response lost")
        return [{"success": True, "successOrderId": "order-9"}]


@dataclass(frozen=True)
class Plan:
    plan_id: str = "wv-dustcase"
    dust_close_max_quote: Decimal = Decimal("10")


def run_close(
    gateway: Gateway,
    *,
    store: IntentStore | None = None,
    reconciler: Reconciler | None = None,
    owned: str = "1",
    maker_reason: str = "maker_completed_with_residual",
):
    events: list[dict[str, object]] = []
    result = close_dust_position_once(
        gateway=gateway,
        store=store or IntentStore(),
        plan=Plan(),
        cycle=2,
        symbol="BTC",
        position_side="long",
        owned_quantity=Decimal(owned),
        amount_step=Decimal("0.1"),
        maker_reason=maker_reason,
        reconciler=reconciler or Reconciler(),
        now_ms=lambda: 1_000,
        sleep=lambda _seconds: None,
        emit=lambda event, **fields: events.append({"event": event, **fields}),
    )
    return result, events


def test_verified_current_execution_dust_closes_by_position_id_once() -> None:
    gateway = Gateway()
    result, events = run_close(gateway)

    assert result.flat is True
    assert result.report is not None and result.report.taker_count == 1
    assert gateway.close_calls == [("BTC", "position-7")]
    assert [row["event"] for row in events] == [
        "dust_close_detected",
        "market_close_intent_persisted",
        "market_close_accepted",
        "market_close_verified",
    ]


def test_unowned_or_large_position_never_uses_market_close() -> None:
    unowned, _ = run_close(Gateway(), owned="0.5")
    large_gateway = Gateway(quantity="2", quote="20")
    large, _ = run_close(large_gateway, owned="2")

    assert unowned.reason == "position_not_owned_by_execution"
    assert large.reason == "position_not_dust"
    assert large_gateway.close_calls == []


def test_ordinary_post_only_rejection_never_downgrades_to_taker() -> None:
    gateway = Gateway()
    result, _ = run_close(gateway, maker_reason="post_only_rejected")

    assert result.reason == "maker_failure_not_dust_eligible"
    assert gateway.close_calls == []


def test_below_exchange_minimum_can_close_even_above_quote_threshold() -> None:
    gateway = Gateway(quantity="0.5", quote="50", minimum="1")
    result, _ = run_close(gateway, owned="0.5", maker_reason="below_minimum")

    assert result.flat is True
    assert gateway.close_calls == [("BTC", "position-7")]


def test_active_regular_or_trigger_order_blocks_market_close() -> None:
    gateway = Gateway()
    gateway.trigger_orders = [{"id": "trigger-1"}]
    result, _ = run_close(gateway)

    assert result.uncertain is True
    assert result.reason == "active_order_blocks_dust_close"
    assert gateway.close_calls == []


def test_transport_uncertainty_that_lands_is_not_resubmitted() -> None:
    gateway = Gateway(close_outcome="transport_after_apply")
    store = IntentStore()
    first, _ = run_close(gateway, store=store, reconciler=Reconciler(visible=False))
    second, _ = run_close(gateway, store=store)

    assert first.flat is True and first.reason == "audit_pending"
    assert second.flat is True and second.attempted is False
    assert gateway.close_calls == [("BTC", "position-7")]


def test_position_read_timeouts_after_submission_never_count_as_flat() -> None:
    gateway = Gateway(close_outcome="verification_timeout")
    result, _ = run_close(gateway)

    assert result.flat is False
    assert result.uncertain is True
    assert result.reason == "market_close_not_flat"
    assert gateway.close_calls == [("BTC", "position-7")]


def test_accounting_accepts_only_explicit_verified_dust_taker_policy() -> None:
    dust_leg = {
        "accounting_verified": True,
        "maker_only": False,
        "liquidity_policy_satisfied": True,
        "dust_market_close": True,
        "fill_count": 1,
        "taker_count": 1,
        "quote_volume": "5",
    }
    ordinary_taker = {**dust_leg, "liquidity_policy_satisfied": False, "dust_market_close": False}

    accepted = accounting_summary([dust_leg])
    rejected = accounting_summary([ordinary_taker])

    assert accepted["maker_only"] is False
    assert accepted["liquidity_policy_satisfied"] is True
    assert rejected["liquidity_policy_satisfied"] is False
