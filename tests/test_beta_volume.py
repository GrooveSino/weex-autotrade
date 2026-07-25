from __future__ import annotations

import threading
from dataclasses import replace
from decimal import ROUND_DOWN, Decimal

import ccxt
import pytest

from weex_cli.adaptive_executor import TargetExecutionResult, VenueOrder
from weex_cli.adaptive_maker import MarketSnapshot
from weex_cli.beta_allocation import BetaAllocation
from weex_cli.beta_volume import (
    BetaVolumePlan,
    BetaVolumePlanStore,
    LiveBetaVolumeService,
    beta_volume_confirmation,
    select_leverage,
)
from weex_cli.beta_volume_workflow import BetaVolumeApplication, BetaVolumePlanRequest
from weex_cli.errors import SafetyError, ValidationError
from weex_cli.execution_reconciliation import LegFillReport, LegFillRequest


class Gateway:
    def __init__(self) -> None:
        self.positions_by_symbol: dict[str, list[dict]] = {"BTC": [], "ETH": []}
        self.orders_by_symbol: dict[str, list[dict]] = {"BTC": [], "ETH": []}
        self.leverage_by_symbol = {"BTC": 1, "ETH": 1}
        self.margin_mode_by_symbol = {"BTC": "isolated", "ETH": "isolated"}
        self.leverage_updates: list[tuple[str, int, str]] = []
        self.margin_mode_updates: list[tuple[str, str]] = []

    def order_book(self, symbol: str, limit: int = 5) -> dict:
        mid = Decimal("100") if symbol == "BTC" else Decimal("50")
        return {"bids": [[float(mid - 1), 10]], "asks": [[float(mid + 1), 10]]}

    def amount_step(self, symbol: str) -> Decimal:
        return Decimal("0.1") if symbol == "BTC" else Decimal("0.2")

    def amount_to_precision(self, symbol: str, amount: Decimal) -> Decimal:
        step = self.amount_step(symbol)
        return (amount / step).to_integral_value(rounding=ROUND_DOWN) * step

    def account_balance_rows(self, mode: str) -> list[dict]:
        return [{"asset": "USDT", "availableBalance": "1000"}]

    def positions(self, mode: str, symbol: str | None = None) -> list[dict]:
        return list(self.positions_by_symbol[symbol or "BTC"])

    def open_orders(self, symbol: str | None = None, *, mode: str = "live", trigger: bool = False) -> list[dict]:
        return list(self.orders_by_symbol[symbol or "BTC"])

    def algo_orders(self, symbol: str | None = None) -> list[dict]:
        return []

    def leverage(self, symbol: str) -> dict:
        value = self.leverage_by_symbol[symbol]
        return {
            "marginMode": self.margin_mode_by_symbol[symbol],
            "longLeverage": value,
            "shortLeverage": value,
        }

    def configure_margin_mode(self, symbol: str, margin_mode: str) -> dict:
        self.margin_mode_updates.append((symbol, margin_mode))
        self.margin_mode_by_symbol[symbol] = margin_mode
        return {"status": "accepted"}

    def configure_leverage(self, symbol: str, leverage: int, margin_mode: str) -> dict:
        self.leverage_updates.append((symbol, leverage, margin_mode))
        self.leverage_by_symbol[symbol] = leverage
        return {"status": "accepted"}

    def order_history(
        self,
        mode: str,
        symbol: str | None = None,
        limit: int = 100,
        start_time: int | None = None,
        end_time: int | None = None,
    ) -> list[dict]:
        return []


class ClosableGateway(Gateway):
    def __init__(self) -> None:
        super().__init__()
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


class HistoryIdentityGateway(Gateway):
    def __init__(self, plan_id: str) -> None:
        super().__init__()
        self.plan_id = plan_id

    def order_history(
        self,
        mode: str,
        symbol: str | None = None,
        limit: int = 100,
        start_time: int | None = None,
        end_time: int | None = None,
    ) -> list[dict]:
        suffixes = ("bo", "eo", "btcc1", "ethc1")
        return [
            {
                "orderId": f"order-{suffix}",
                "clientOrderId": f"{self.plan_id}-r001-{suffix}-001",
                "status": "FILLED",
                "executedQty": "1",
            }
            for suffix in suffixes
        ]


class RealisticGateway(Gateway):
    def order_book(self, symbol: str, limit: int = 5) -> dict:
        mid = Decimal("64000") if symbol == "BTC" else Decimal("1840")
        return {"bids": [[float(mid - 1), 10]], "asks": [[float(mid + 1), 10]]}

    def amount_step(self, symbol: str) -> Decimal:
        return Decimal("0.0001") if symbol == "BTC" else Decimal("0.001")


class BalanceSequenceGateway(Gateway):
    def __init__(self, balances: list[str]) -> None:
        super().__init__()
        self.balances = iter(balances)
        self.margin_mode_by_symbol = {"BTC": "cross", "ETH": "cross"}

    def account_balance_rows(self, mode: str) -> list[dict]:
        return [{"asset": "USDT", "availableBalance": next(self.balances)}]


class DeniedBalanceGateway(BalanceSequenceGateway):
    def configure_leverage(self, symbol: str, leverage: int, margin_mode: str) -> dict:
        self.leverage_updates.append((symbol, leverage, margin_mode))
        raise ccxt.PermissionDenied("leverage mutation denied")


class ConfigurationFailureGateway(Gateway):
    def __init__(self, failure: str) -> None:
        super().__init__()
        self.failure = failure

    def configure_margin_mode(self, symbol: str, margin_mode: str) -> dict:
        self.margin_mode_updates.append((symbol, margin_mode))
        if self.failure == "margin_denied":
            raise ccxt.PermissionDenied("margin mutation denied")
        if self.failure != "margin_mismatch":
            self.margin_mode_by_symbol[symbol] = margin_mode
        if self.failure == "ambiguous_applied":
            raise ccxt.RequestTimeout("margin response lost")
        return {"status": "accepted"}

    def configure_leverage(self, symbol: str, leverage: int, margin_mode: str) -> dict:
        self.leverage_updates.append((symbol, leverage, margin_mode))
        if self.failure != "leverage_mismatch":
            self.leverage_by_symbol[symbol] = leverage
        if self.failure == "ambiguous_applied":
            raise ccxt.RequestTimeout("leverage response lost")
        return {"status": "accepted"}


class FlakyPreflightGateway(Gateway):
    def __init__(self, failures: int) -> None:
        super().__init__()
        self.failures = failures
        self.order_book_calls = 0

    def order_book(self, symbol: str, limit: int = 5) -> dict:
        self.order_book_calls += 1
        if self.order_book_calls <= self.failures:
            raise ccxt.NetworkError("temporary exchangeInfo failure")
        return super().order_book(symbol, limit)


class FlakyCycleGateway(Gateway):
    def __init__(self, *, book_failures: int = 0, order_failures: int = 0) -> None:
        super().__init__()
        self.book_failures = book_failures
        self.order_failures = order_failures
        self.order_book_calls = 0
        self.open_order_calls = 0

    def order_book(self, symbol: str, limit: int = 5) -> dict:
        self.order_book_calls += 1
        if self.book_failures:
            self.book_failures -= 1
            raise ccxt.RequestTimeout("temporary cycle book timeout")
        return super().order_book(symbol, limit)

    def open_orders(self, symbol: str | None = None, *, mode: str = "live", trigger: bool = False) -> list[dict]:
        self.open_order_calls += 1
        if self.order_failures:
            self.order_failures -= 1
            raise ccxt.RequestTimeout("temporary post-leg order timeout")
        return super().open_orders(symbol, mode=mode, trigger=trigger)


class FlakyLeverageGateway(Gateway):
    def __init__(self) -> None:
        super().__init__()
        self.margin_mode_by_symbol = {"BTC": "cross", "ETH": "cross"}
        self.leverage_calls = 0

    def account_balance_rows(self, mode: str) -> list[dict]:
        return [{"asset": "USDT", "availableBalance": "60"}]

    def leverage(self, symbol: str) -> dict:
        self.leverage_calls += 1
        if self.leverage_calls in {1, 3, 5}:
            raise ccxt.RequestTimeout("temporary leverage timeout")
        return super().leverage(symbol)


class Provider:
    def __init__(self, allocation: BetaAllocation) -> None:
        self.allocation = allocation

    def get(self) -> BetaAllocation:
        return self.allocation


class ImmediateVenue:
    def __init__(self, symbol: str, position_side: str) -> None:
        self.symbol = symbol
        self.position_side = position_side
        self.position = 0.0
        self.time = 0
        self.order: VenueOrder | None = None

    @property
    def now_ms(self) -> int:
        return self.time

    def snapshot(self) -> MarketSnapshot:
        mid = 100.0 if self.symbol == "BTC" else 50.0
        return MarketSnapshot(self.time, mid - 1, mid + 1, 10, 10, 10, 10, 1, 1)

    def position_quantity(self) -> float:
        return self.position

    def wait_for_submission_slot(self) -> None:
        return None

    def submit_post_only(self, side: str, quantity: float, price: float, client_order_id: str) -> VenueOrder:
        self.position += quantity if side == "buy" else -quantity
        self.order = VenueOrder(
            order_id=client_order_id,
            client_order_id=client_order_id,
            side=side,  # type: ignore[arg-type]
            price=price,
            quantity=quantity,
            filled_quantity=quantity,
            cumulative_quote=quantity * price,
            status="filled",
            post_only=True,
            maker=True,
        )
        return self.order

    def fetch_order(self, order_id: str, client_order_id: str) -> VenueOrder:
        assert self.order is not None
        return self.order

    def cancel_order(self, order_id: str, client_order_id: str) -> VenueOrder:
        raise AssertionError("immediate fills are never canceled")

    def advance(self, milliseconds: int) -> None:
        self.time += milliseconds


class SafeStopVenue(ImmediateVenue):
    def __init__(self, symbol: str, position_side: str) -> None:
        super().__init__(symbol, position_side)
        self.cancel_all_calls = 0

    def cancel_all_and_verify(self) -> bool:
        self.cancel_all_calls += 1
        return True


class OneShotPositionTimeoutVenue(ImmediateVenue):
    def __init__(self, symbol: str, position_side: str) -> None:
        super().__init__(symbol, position_side)
        self.fail_next_position_read = False

    def position_quantity(self) -> float:
        if self.fail_next_position_read:
            self.fail_next_position_read = False
            raise ccxt.RequestTimeout("temporary position timeout")
        return super().position_quantity()


class RejectingVenue(ImmediateVenue):
    def submit_post_only(self, side: str, quantity: float, price: float, client_order_id: str) -> VenueOrder:
        return VenueOrder(
            order_id="rejected",
            client_order_id=client_order_id,
            side=side,  # type: ignore[arg-type]
            price=price,
            quantity=quantity,
            filled_quantity=0,
            cumulative_quote=0,
            status="rejected",
            post_only=True,
            maker=None,
        )


class UncertainVenue(ImmediateVenue):
    def submit_post_only(self, side: str, quantity: float, price: float, client_order_id: str) -> VenueOrder:
        raise ConnectionError("submission outcome is unknown")


class SparseTerminalVenue(ImmediateVenue):
    def submit_post_only(self, side: str, quantity: float, price: float, client_order_id: str) -> VenueOrder:
        self.position += quantity if side == "buy" else -quantity
        self.order = VenueOrder(
            order_id=client_order_id,
            client_order_id=client_order_id,
            side=side,  # type: ignore[arg-type]
            price=price,
            quantity=quantity,
            filled_quantity=0,
            cumulative_quote=0,
            status="filled",
            post_only=True,
            maker=None,
        )
        return self.order


class BarrierVenue(ImmediateVenue):
    def __init__(
        self,
        symbol: str,
        position_side: str,
        open_barrier: threading.Barrier,
        close_barrier: threading.Barrier,
        thread_ids: dict[str, set[int]],
    ) -> None:
        super().__init__(symbol, position_side)
        self.open_barrier = open_barrier
        self.close_barrier = close_barrier
        self.thread_ids = thread_ids
        self.open_waited = False
        self.close_waited = False

    def submit_post_only(self, side: str, quantity: float, price: float, client_order_id: str) -> VenueOrder:
        opening = (self.symbol == "BTC" and side == "buy") or (self.symbol == "ETH" and side == "sell")
        if opening and not self.open_waited:
            self.open_waited = True
            self.thread_ids["open"].add(threading.get_ident())
            self.open_barrier.wait(timeout=2)
        if not opening and not self.close_waited:
            self.close_waited = True
            self.thread_ids["close"].add(threading.get_ident())
            self.close_barrier.wait(timeout=2)
        return super().submit_post_only(side, quantity, price, client_order_id)


class WatchdogVenue(ImmediateVenue):
    def __init__(self, symbol: str, position_side: str, release_btc: threading.Event) -> None:
        super().__init__(symbol, position_side)
        self.release_btc = release_btc

    def submit_post_only(self, side: str, quantity: float, price: float, client_order_id: str) -> VenueOrder:
        if self.symbol == "BTC" and side == "buy" and self.position == 0:
            assert self.release_btc.wait(timeout=1)
        return super().submit_post_only(side, quantity, price, client_order_id)


class DeterministicReconciler:
    def reconcile(self, request: LegFillRequest) -> LegFillReport:
        price = Decimal("100") if request.symbol == "BTC" else Decimal("50")
        return LegFillReport(
            status="verified",
            source_complete=True,
            fill_count=1,
            order_count=1,
            executed_quantity=request.expected_quantity,
            quote_volume=request.expected_quantity * price,
            maker_only=True,
            maker_count=1,
            taker_count=0,
            unknown_liquidity_count=0,
            commission_by_asset={"USDT": Decimal("0.01")},
            realized_pnl=Decimal("0"),
        )


class TakerOnEthOpenReconciler(DeterministicReconciler):
    def reconcile(self, request: LegFillRequest) -> LegFillReport:
        report = super().reconcile(request)
        if request.symbol == "ETH" and request.action == "open":
            return replace(report, status="taker_fill_detected", maker_only=False, maker_count=0, taker_count=1)
        return report


class DelayedBtcOpenReconciler(DeterministicReconciler):
    def __init__(self) -> None:
        self.requests: list[LegFillRequest] = []
        self.delayed = False

    def reconcile(self, request: LegFillRequest) -> LegFillReport:
        self.requests.append(request)
        report = super().reconcile(request)
        if request.symbol == "BTC" and request.action == "open" and not self.delayed:
            self.delayed = True
            return replace(
                report,
                status="fills_not_visible",
                source_complete=True,
                fill_count=0,
                order_count=0,
                executed_quantity=Decimal(0),
                quote_volume=Decimal(0),
                maker_only=False,
                maker_count=0,
            )
        return report


class InvisibleBtcOpenReconciler(DeterministicReconciler):
    def reconcile(self, request: LegFillRequest) -> LegFillReport:
        report = super().reconcile(request)
        if request.symbol == "BTC" and request.action == "open":
            return replace(
                report,
                status="fills_not_visible",
                source_complete=True,
                fill_count=0,
                order_count=0,
                executed_quantity=Decimal(0),
                quote_volume=Decimal(0),
                maker_only=False,
                maker_count=0,
            )
        return report


class DelayedTakerBtcOpenReconciler(DelayedBtcOpenReconciler):
    def reconcile(self, request: LegFillRequest) -> LegFillReport:
        delayed_before = self.delayed
        report = super().reconcile(request)
        if delayed_before and request.symbol == "BTC" and request.action == "open":
            return replace(report, status="taker_fill_detected", maker_only=False, maker_count=0, taker_count=1)
        return report


class TwiceDelayedBtcOpenReconciler(DeterministicReconciler):
    def __init__(self) -> None:
        self.open_calls = 0

    def reconcile(self, request: LegFillRequest) -> LegFillReport:
        report = super().reconcile(request)
        if request.symbol == "BTC" and request.action == "open":
            self.open_calls += 1
            if self.open_calls <= 2:
                return replace(
                    report,
                    status="fills_not_visible",
                    source_complete=True,
                    fill_count=0,
                    order_count=0,
                    executed_quantity=Decimal(0),
                    quote_volume=Decimal(0),
                    maker_only=False,
                    maker_count=0,
                )
        return report


@pytest.fixture
def allocation() -> BetaAllocation:
    return BetaAllocation(
        beta=Decimal("1"),
        btc_long_weight=Decimal("0.5"),
        eth_short_weight=Decimal("0.5"),
        version="beta-v1:123",
        as_of_ms=123,
        confidence=Decimal("0.8"),
        confidence_threshold=Decimal("0.65"),
        source="test",
    )


def test_plan_applies_beta_after_halving_turnover_and_rounds_up(allocation: BetaAllocation) -> None:
    plan = BetaVolumePlan.create(
        Gateway(),
        allocation,
        target_turnover_quote="200",
        max_position_quote="1200",
        timeout_seconds=120,
        now_ms=1000,
    )

    assert plan.opening_budget_quote == Decimal("100")
    assert plan.btc.allocated_quote == Decimal("50.0")
    assert plan.eth.allocated_quote == Decimal("50.0")
    assert plan.btc.quantity == Decimal("0.5")
    assert plan.eth.quantity == Decimal("1.0")
    assert plan.estimated_turnover_quote == Decimal("200.00")
    assert "POST_ONLY" in beta_volume_confirmation(plan)


def test_application_defaults_to_auto_leverage_and_short_execution_command(tmp_path, allocation) -> None:
    payload = BetaVolumeApplication(Gateway(), BetaVolumePlanStore(tmp_path)).create_plan(
        BetaVolumePlanRequest(target_turnover_quote="200"),
        Provider(allocation),  # type: ignore[arg-type]
    )

    assert payload["schema_version"] == 3
    assert payload["plan"]["leverage"] == "auto"
    assert payload["account_readiness"]["planned_leverage"] == 1
    assert "TARGET_" not in payload["confirm"]
    assert payload["execute_command"].endswith(f"--confirm '{payload['confirm']}'")


def test_plan_rejects_target_below_both_minimum_allocations(allocation: BetaAllocation) -> None:
    with pytest.raises(ValidationError, match="current minimum is approximately 40.00 USDT"):
        BetaVolumePlan.create(
            Gateway(),
            allocation,
            target_turnover_quote="10",
            max_position_quote="1200",
            timeout_seconds=120,
        )


def test_pair_quantity_solver_minimizes_joint_overshoot() -> None:
    allocation = BetaAllocation(
        beta=Decimal("0.5"),
        btc_long_weight=Decimal("0.6666666666666666666666666667"),
        eth_short_weight=Decimal("0.3333333333333333333333333333"),
        version="beta-v1:123",
        as_of_ms=123,
        confidence=Decimal("0.8"),
        confidence_threshold=Decimal("0.65"),
        source="test",
    )

    plan = BetaVolumePlan.create(
        RealisticGateway(),
        allocation,
        target_turnover_quote="20",
        max_position_quote="1200",
        timeout_seconds=120,
    )

    assert plan.btc.quantity == Decimal("0.0001")
    assert plan.eth.quantity == Decimal("0.002")
    assert plan.estimated_turnover_quote == Decimal("20.16")


def test_leverage_is_bound_to_plan_confirmation_and_margin_requirement(allocation: BetaAllocation) -> None:
    plan = BetaVolumePlan.create(
        Gateway(),
        allocation,
        target_turnover_quote="200",
        max_position_quote="1200",
        timeout_seconds=120,
        leverage=5,
    )

    assert plan.leverage == 5
    assert plan.margin_mode == "isolated"
    assert plan.required_available_quote == Decimal("24.000")
    assert "LEVERAGE_5X POST_ONLY" in beta_volume_confirmation(plan)


def test_v5_reverse_plan_uses_fixed_cross_leverage_and_persisted_sides(
    tmp_path,
    allocation: BetaAllocation,
) -> None:
    gateway = Gateway()
    plan = BetaVolumePlan.create(
        gateway,
        allocation,
        target_turnover_quote="200",
        round_turnover_quote="200",
        max_position_quote="1200",
        timeout_seconds=120,
        leverage=400,
        margin_mode="cross",
        direction="btc_short_eth_long",
        now_ms=1000,
    )
    store = BetaVolumePlanStore(tmp_path)
    store.save(plan)
    events: list[dict[str, object]] = []

    result = LiveBetaVolumeService(
        gateway,
        Provider(allocation),  # type: ignore[arg-type]
        store,
        venue_factory=lambda unused, symbol, side: ImmediateVenue(symbol, side),  # type: ignore[arg-type]
        gateway_factory=Gateway,
        reconciler_factory=lambda unused: DeterministicReconciler(),
        event_sink=events.append,  # type: ignore[arg-type]
        now_ms=lambda: 1000,
        sleep=lambda _seconds: None,
    ).execute(plan)

    assert plan.schema_version == 5
    assert (plan.btc.position_side, plan.btc.opening_side, plan.btc.closing_side) == ("short", "sell", "buy")
    assert (plan.eth.position_side, plan.eth.opening_side, plan.eth.closing_side) == ("long", "buy", "sell")
    assert gateway.margin_mode_updates == [("BTC", "cross"), ("ETH", "cross")]
    assert gateway.leverage_updates == [("BTC", 400, "cross"), ("ETH", 400, "cross")]
    assert result["status"] == "completed"
    assert result["strategy"] == "btc_short_eth_long"
    opening_sides = {
        str(event["symbol"]): str(event["side"])
        for event in events
        if event["event"] == "leg_started" and event["action"] == "open"
    }
    assert opening_sides == {"BTC": "sell", "ETH": "buy"}


def test_legacy_plan_cannot_gain_market_dust_close_during_execution(monkeypatch, tmp_path, allocation) -> None:
    plan = replace(
        BetaVolumePlan.create(
            Gateway(),
            allocation,
            target_turnover_quote="200",
            max_position_quote="1200",
            timeout_seconds=120,
            now_ms=1000,
        ),
        schema_version=4,
    )
    service = LiveBetaVolumeService(Gateway(), None, BetaVolumePlanStore(tmp_path))  # type: ignore[arg-type]
    monkeypatch.setattr(
        "weex_cli.beta_volume.close_dust_position_once",
        lambda **_kwargs: pytest.fail("legacy tasks must not call closePositions"),
    )

    result = service._close_dust_if_eligible(
        plan,
        1,
        plan.btc,
        None,  # type: ignore[arg-type]
        Decimal("1"),
        "below_minimum",
        1,
    )

    assert result is None


def test_auto_leverage_uses_wallet_round_size_and_safety_buffer() -> None:
    assert select_leverage("auto", Decimal("250"), Decimal("10")) == 30
    assert select_leverage("auto", Decimal("250"), Decimal("1000")) == 1
    with pytest.raises(SafetyError, match="above the 99x automatic limit"):
        select_leverage("auto", Decimal("1000"), Decimal("10"))
    with pytest.raises(SafetyError, match="requires at least 30x"):
        select_leverage(20, Decimal("250"), Decimal("10"))


def test_plan_rejects_unsupported_leverage_or_margin(allocation: BetaAllocation) -> None:
    with pytest.raises(ValidationError, match="integer between 1 and 400"):
        BetaVolumePlan.create(
            Gateway(),
            allocation,
            target_turnover_quote="200",
            max_position_quote="1200",
            timeout_seconds=120,
            leverage=401,
        )
    with pytest.raises(ValidationError, match="margin_mode must be isolated or cross"):
        BetaVolumePlan.create(
            Gateway(),
            allocation,
            target_turnover_quote="200",
            max_position_quote="1200",
            timeout_seconds=120,
            margin_mode="portfolio",
        )


def test_plan_store_round_trip_and_rejects_reexecution(tmp_path, allocation: BetaAllocation) -> None:
    plan = BetaVolumePlan.create(
        Gateway(),
        allocation,
        target_turnover_quote="200",
        max_position_quote="1200",
        timeout_seconds=120,
        now_ms=1000,
    )
    store = BetaVolumePlanStore(tmp_path)
    store.save(plan)

    assert store.load(plan.plan_id) == (plan, "planned")
    store.save(plan, state="executing")
    service = LiveBetaVolumeService(Gateway(), Provider(allocation), store, now_ms=lambda: 1000)  # type: ignore[arg-type]
    with pytest.raises(SafetyError, match="pristine planned state"):
        service.execute(plan)


def test_plan_store_create_and_execution_claim_are_one_shot(tmp_path, allocation: BetaAllocation) -> None:
    plan = BetaVolumePlan.create(
        Gateway(),
        allocation,
        target_turnover_quote="200",
        max_position_quote="1200",
        timeout_seconds=120,
        now_ms=1000,
    )
    store = BetaVolumePlanStore(tmp_path)

    store.create(plan)
    with pytest.raises(SafetyError, match="already exists"):
        store.create(plan)

    store.claim_for_execution(plan)
    assert store.load(plan.plan_id)[1] == "executing"
    with pytest.raises(SafetyError, match="pristine planned state"):
        store.claim_for_execution(plan)


def test_read_only_preflight_retries_before_claiming_plan(tmp_path, allocation: BetaAllocation) -> None:
    plan = BetaVolumePlan.create(
        Gateway(),
        allocation,
        target_turnover_quote="200",
        max_position_quote="1200",
        timeout_seconds=120,
        now_ms=1000,
    )
    store = BetaVolumePlanStore(tmp_path)
    store.save(plan)
    gateway = FlakyPreflightGateway(failures=2)
    delays: list[float] = []

    result = LiveBetaVolumeService(
        gateway,
        Provider(allocation),  # type: ignore[arg-type]
        store,
        venue_factory=lambda unused, symbol, side: ImmediateVenue(symbol, side),  # type: ignore[arg-type]
        gateway_factory=Gateway,
        reconciler_factory=lambda unused: DeterministicReconciler(),
        now_ms=lambda: 1000,
        sleep=delays.append,
    ).execute(plan)

    assert result["status"] == "completed"
    assert delays == [1, 2]
    assert store.load(plan.plan_id)[1] == "completed"


def test_exhausted_read_only_preflight_does_not_consume_plan(tmp_path, allocation: BetaAllocation) -> None:
    plan = BetaVolumePlan.create(
        Gateway(),
        allocation,
        target_turnover_quote="200",
        max_position_quote="1200",
        timeout_seconds=120,
        now_ms=1000,
    )
    store = BetaVolumePlanStore(tmp_path)
    store.save(plan)
    gateway = FlakyPreflightGateway(failures=8)
    delays: list[float] = []

    with pytest.raises(ccxt.NetworkError, match="temporary exchangeInfo failure"):
        LiveBetaVolumeService(
            gateway,
            Provider(allocation),  # type: ignore[arg-type]
            store,
            now_ms=lambda: 1000,
            sleep=delays.append,
        ).execute(plan)

    assert delays == [1, 2, 4, 8, 8, 8, 8]
    assert store.load(plan.plan_id)[1] == "planned"


def test_cycle_sizing_recovers_from_transient_book_timeout(tmp_path, allocation: BetaAllocation) -> None:
    gateway = Gateway()
    plan = BetaVolumePlan.create(
        gateway,
        allocation,
        target_turnover_quote="200",
        max_position_quote="1200",
        timeout_seconds=120,
        now_ms=1000,
    )
    store = BetaVolumePlanStore(tmp_path)
    store.save(plan)
    lane_gateways = [FlakyCycleGateway(book_failures=1), FlakyCycleGateway()]
    events: list[dict] = []
    delays: list[float] = []

    result = LiveBetaVolumeService(
        gateway,
        Provider(allocation),  # type: ignore[arg-type]
        store,
        venue_factory=lambda unused, symbol, side: ImmediateVenue(symbol, side),  # type: ignore[arg-type]
        gateway_factory=lambda: lane_gateways.pop(0),  # type: ignore[arg-type]
        reconciler_factory=lambda unused: DeterministicReconciler(),
        now_ms=lambda: 1000,
        sleep=delays.append,
        event_sink=lambda event: events.append(dict(event)),
    ).execute(plan)

    assert result["status"] == "completed"
    assert delays[0] == 1
    assert any(event["event"] == "cycle_sizing_retry" for event in events)


def test_post_leg_order_observation_recovers_before_continuing(tmp_path, allocation: BetaAllocation) -> None:
    gateway = Gateway()
    plan = BetaVolumePlan.create(
        gateway,
        allocation,
        target_turnover_quote="200",
        max_position_quote="1200",
        timeout_seconds=120,
        now_ms=1000,
    )
    store = BetaVolumePlanStore(tmp_path)
    store.save(plan)
    created = [FlakyCycleGateway(order_failures=1), FlakyCycleGateway(order_failures=1)]
    lanes = list(created)
    events: list[dict] = []

    result = LiveBetaVolumeService(
        gateway,
        Provider(allocation),  # type: ignore[arg-type]
        store,
        venue_factory=lambda unused, symbol, side: ImmediateVenue(symbol, side),  # type: ignore[arg-type]
        gateway_factory=lambda: created.pop(0),  # type: ignore[arg-type]
        reconciler_factory=lambda unused: DeterministicReconciler(),
        now_ms=lambda: 1000,
        sleep=lambda _: None,
        event_sink=lambda event: events.append(dict(event)),
    ).execute(plan)

    assert result["status"] == "completed"
    assert all(lane.open_order_calls >= 2 for lane in lanes)
    assert any(
        event["event"] == "leg_waiting" and event.get("waiting_for") == "order_observation_retry" for event in events
    )


def test_leverage_reads_retry_while_each_configuration_mutation_stays_single_shot(
    tmp_path, allocation: BetaAllocation
) -> None:
    gateway = FlakyLeverageGateway()
    plan = BetaVolumePlan.create(
        Gateway(),
        allocation,
        target_turnover_quote="200",
        max_position_quote="1200",
        timeout_seconds=120,
        now_ms=1000,
    )
    store = BetaVolumePlanStore(tmp_path)
    store.save(plan)

    result = LiveBetaVolumeService(
        gateway,
        Provider(allocation),  # type: ignore[arg-type]
        store,
        venue_factory=lambda unused, symbol, side: ImmediateVenue(symbol, side),  # type: ignore[arg-type]
        gateway_factory=Gateway,
        reconciler_factory=lambda unused: DeterministicReconciler(),
        now_ms=lambda: 1000,
        sleep=lambda _: None,
    ).execute(plan)

    assert result["status"] == "completed"
    assert gateway.margin_mode_updates == [("BTC", "isolated"), ("ETH", "isolated")]
    assert gateway.leverage_updates == [("BTC", 2, "isolated"), ("ETH", 2, "isolated")]
    assert gateway.leverage_calls >= 6


def test_confidence_metadata_round_trips_without_changing_confirmation(tmp_path, allocation: BetaAllocation) -> None:
    overridden = BetaAllocation(
        beta=allocation.beta,
        btc_long_weight=allocation.btc_long_weight,
        eth_short_weight=allocation.eth_short_weight,
        version="beta-v1-low-confidence:123",
        as_of_ms=allocation.as_of_ms,
        confidence=Decimal("0.59"),
        confidence_threshold=allocation.confidence_threshold,
        source=allocation.source,
        confidence_override=True,
    )
    plan = BetaVolumePlan.create(
        Gateway(),
        overridden,
        target_turnover_quote="200",
        max_position_quote="1200",
        timeout_seconds=120,
        now_ms=1000,
    )
    store = BetaVolumePlanStore(tmp_path)
    store.save(plan)

    restored, state = store.load(plan.plan_id)

    assert state == "planned"
    assert restored.allocation.confidence_override is True
    assert "LOW_CONFIDENCE_OVERRIDE" not in beta_volume_confirmation(restored)
    assert beta_volume_confirmation(restored).endswith("LEVERAGE_AUTO POST_ONLY")


def test_preflight_rejects_plan_after_fifteen_minutes(tmp_path, allocation: BetaAllocation) -> None:
    plan = BetaVolumePlan.create(
        Gateway(),
        allocation,
        target_turnover_quote="200",
        max_position_quote="1200",
        timeout_seconds=120,
        now_ms=1000,
    )
    service = LiveBetaVolumeService(
        Gateway(),
        Provider(allocation),
        BetaVolumePlanStore(tmp_path),
        now_ms=lambda: 902_000,  # type: ignore[arg-type]
    )

    with pytest.raises(SafetyError, match="plan expired"):
        service.preflight(plan)


def test_live_service_executes_signed_four_leg_pair_and_ends_flat(tmp_path, allocation: BetaAllocation) -> None:
    gateway = Gateway()
    plan = BetaVolumePlan.create(
        gateway,
        allocation,
        target_turnover_quote="200",
        max_position_quote="1200",
        timeout_seconds=120,
        now_ms=1000,
    )
    store = BetaVolumePlanStore(tmp_path)
    store.save(plan)
    venues: dict[str, ImmediateVenue] = {}

    def factory(unused_gateway: Gateway, symbol: str, position_side: str) -> ImmediateVenue:
        venues.setdefault(symbol, ImmediateVenue(symbol, position_side))
        return venues[symbol]

    service = LiveBetaVolumeService(
        gateway,
        Provider(allocation),  # type: ignore[arg-type]
        store,
        venue_factory=factory,  # type: ignore[arg-type]
        now_ms=lambda: 1000,
        gateway_factory=Gateway,
        reconciler_factory=lambda unused_gateway: DeterministicReconciler(),
        sleep=lambda seconds: None,
    )
    result = service.execute(plan)

    assert result["status"] == "completed"
    assert [leg["action"] for leg in result["legs"]] == ["open", "open", "close", "close"]
    assert [leg["side"] for leg in result["legs"]] == ["buy", "sell", "sell", "buy"]
    assert result["executed_quote_volume"] == "200"
    assert result["accounting"]["fill_count"] == 4
    assert result["accounting"]["maker_only"] is True
    assert venues["BTC"].position == pytest.approx(0)
    assert venues["ETH"].position == pytest.approx(0)
    assert store.load(plan.plan_id)[1] == "completed"


def test_open_and_close_phases_are_parallel_and_use_distinct_gateways(tmp_path, allocation: BetaAllocation) -> None:
    gateway = Gateway()
    plan = BetaVolumePlan.create(
        gateway,
        allocation,
        target_turnover_quote="200",
        max_position_quote="1200",
        timeout_seconds=120,
        now_ms=1000,
    )
    store = BetaVolumePlanStore(tmp_path)
    store.save(plan)
    open_barrier = threading.Barrier(2)
    close_barrier = threading.Barrier(2)
    thread_ids = {"open": set(), "close": set()}
    lane_gateways: list[Gateway] = []

    def gateway_factory() -> Gateway:
        lane = Gateway()
        lane_gateways.append(lane)
        return lane

    def venue_factory(unused_gateway: Gateway, symbol: str, position_side: str) -> BarrierVenue:
        return BarrierVenue(symbol, position_side, open_barrier, close_barrier, thread_ids)

    result = LiveBetaVolumeService(
        gateway,
        Provider(allocation),  # type: ignore[arg-type]
        store,
        venue_factory=venue_factory,  # type: ignore[arg-type]
        gateway_factory=gateway_factory,  # type: ignore[arg-type]
        reconciler_factory=lambda unused_gateway: DeterministicReconciler(),
        now_ms=lambda: 1000,
        sleep=lambda seconds: None,
    ).execute(plan)

    assert result["status"] == "completed"
    assert len(lane_gateways) == 2
    assert lane_gateways[0] is not lane_gateways[1]
    assert len(thread_ids["open"]) == 2
    assert len(thread_ids["close"]) == 2
    events = [(row["event"], row.get("action")) for row in result["timeline"]]
    first_close = next(index for index, event in enumerate(events) if event == ("leg_started", "close"))
    assert sum(1 for event in events[:first_close] if event == ("leg_completed", "open")) == 2


def test_external_lane_gateways_are_reused_and_not_closed_by_child_service(
    tmp_path, allocation: BetaAllocation
) -> None:
    gateway = Gateway()
    plan = BetaVolumePlan.create(
        gateway,
        allocation,
        target_turnover_quote="200",
        max_position_quote="1200",
        timeout_seconds=120,
        now_ms=1000,
    )
    store = BetaVolumePlanStore(tmp_path)
    store.save(plan)
    btc_gateway = ClosableGateway()
    eth_gateway = ClosableGateway()
    venues: dict[str, ImmediateVenue] = {}

    def venue_factory(unused_gateway: Gateway, symbol: str, position_side: str) -> ImmediateVenue:
        venues.setdefault(symbol, ImmediateVenue(symbol, position_side))
        return venues[symbol]

    result = LiveBetaVolumeService(
        gateway,
        Provider(allocation),  # type: ignore[arg-type]
        store,
        venue_factory=venue_factory,  # type: ignore[arg-type]
        gateway_factory=lambda: pytest.fail("persistent gateways must bypass the factory"),  # type: ignore[arg-type]
        lane_gateways={"BTC": btc_gateway, "ETH": eth_gateway},  # type: ignore[arg-type]
        reconciler_factory=lambda unused_gateway: DeterministicReconciler(),
        now_ms=lambda: 1000,
        sleep=lambda seconds: None,
    ).execute(plan)

    assert result["status"] == "completed"
    assert btc_gateway.close_calls == 0
    assert eth_gateway.close_calls == 0


def test_pair_watchdog_reports_only_the_still_active_leg(tmp_path, allocation: BetaAllocation) -> None:
    gateway = Gateway()
    plan = BetaVolumePlan.create(
        gateway,
        allocation,
        target_turnover_quote="200",
        max_position_quote="1200",
        timeout_seconds=60,
        now_ms=1000,
    )
    store = BetaVolumePlanStore(tmp_path)
    store.save(plan)
    release_btc = threading.Event()
    events: list[dict[str, object]] = []

    def venue_factory(unused_gateway: Gateway, symbol: str, position_side: str) -> WatchdogVenue:
        return WatchdogVenue(symbol, position_side, release_btc)

    def event_sink(event: dict[str, object]) -> None:
        events.append(dict(event))
        if event.get("event") == "pair_wait_progress" and event.get("action") == "open":
            release_btc.set()

    result = LiveBetaVolumeService(
        gateway,
        Provider(allocation),  # type: ignore[arg-type]
        store,
        venue_factory=venue_factory,  # type: ignore[arg-type]
        gateway_factory=Gateway,
        reconciler_factory=lambda unused_gateway: DeterministicReconciler(),
        now_ms=lambda: 1000,
        sleep=lambda seconds: None,
        event_sink=event_sink,  # type: ignore[arg-type]
    ).execute(plan)

    assert result["status"] == "completed"
    heartbeat = next(
        event for event in events if event.get("event") == "pair_wait_progress" and event.get("action") == "open"
    )
    assert heartbeat["active_symbols"] == ("BTC",)
    assert heartbeat["completed_symbols"] == ("ETH",)
    assert 0 <= int(heartbeat["remaining_ms"]) <= 60_000


def test_hold_and_round_gap_follow_confirmed_open_and_flat_boundaries(
    tmp_path,
    allocation: BetaAllocation,
) -> None:
    gateway = Gateway()
    plan = BetaVolumePlan.create(
        gateway,
        allocation,
        target_turnover_quote="400",
        round_turnover_quote="200",
        max_position_quote="1200",
        timeout_seconds=120,
        now_ms=1000,
    )
    store = BetaVolumePlanStore(tmp_path)
    store.save(plan)
    venues: dict[str, ImmediateVenue] = {}
    delays: list[float] = []

    def venue_factory(unused_gateway: Gateway, symbol: str, position_side: str) -> ImmediateVenue:
        venues.setdefault(symbol, ImmediateVenue(symbol, position_side))
        return venues[symbol]

    result = LiveBetaVolumeService(
        gateway,
        Provider(allocation),  # type: ignore[arg-type]
        store,
        venue_factory=venue_factory,  # type: ignore[arg-type]
        gateway_factory=Gateway,
        reconciler_factory=lambda unused_gateway: DeterministicReconciler(),
        now_ms=lambda: 1000,
        sleep=delays.append,
        hold_delay_seconds=lambda round_number: 3,
        round_gap_delay_seconds=lambda round_number: 5,
    ).execute(plan)

    assert result["status"] == "completed"
    assert delays == [3, 5, 3]
    assert [cycle["hold_seconds"] for cycle in result["cycles"]] == [3, 3]
    assert [cycle["round_gap_seconds"] for cycle in result["cycles"]] == [5, 0]

    timeline = result["timeline"]
    for round_number in (1, 2):
        round_events = [(index, row) for index, row in enumerate(timeline) if row.get("round") == round_number]
        open_completed = [
            index for index, row in round_events if row["event"] == "leg_completed" and row.get("action") == "open"
        ]
        open_barrier_verified = next(index for index, row in round_events if row["event"] == "open_barrier_verified")
        hold_started = next(index for index, row in round_events if row["event"] == "hold_started")
        hold_completed = next(index for index, row in round_events if row["event"] == "hold_completed")
        first_close = next(
            index for index, row in round_events if row["event"] == "leg_started" and row.get("action") == "close"
        )
        assert len(open_completed) == 2
        assert max(open_completed) < open_barrier_verified < hold_started < hold_completed < first_close

    first_gap_completed = next(
        index for index, row in enumerate(timeline) if row["event"] == "round_gap_completed" and row.get("round") == 1
    )
    second_cycle_started = next(
        index for index, row in enumerate(timeline) if row["event"] == "cycle_started" and row.get("round") == 2
    )
    assert first_gap_completed < second_cycle_started


def test_hold_wait_requires_both_open_positions_to_reach_their_cycle_targets(
    tmp_path,
    allocation: BetaAllocation,
) -> None:
    gateway = Gateway()
    plan = BetaVolumePlan.create(
        gateway,
        allocation,
        target_turnover_quote="200",
        round_turnover_quote="200",
        max_position_quote="1200",
        timeout_seconds=120,
        now_ms=1000,
    )
    store = BetaVolumePlanStore(tmp_path)
    events: list[dict[str, object]] = []
    venues: dict[str, ImmediateVenue] = {}

    def venue_factory(unused_gateway: Gateway, symbol: str, position_side: str) -> ImmediateVenue:
        venues.setdefault(symbol, ImmediateVenue(symbol, position_side))
        return venues[symbol]

    service = LiveBetaVolumeService(
        gateway,
        Provider(allocation),  # type: ignore[arg-type]
        store,
        venue_factory=venue_factory,  # type: ignore[arg-type]
        gateway_factory=Gateway,
        event_sink=events.append,  # type: ignore[arg-type]
        hold_delay_seconds=lambda round_number: 3,
        now_ms=lambda: 1000,
        sleep=lambda seconds: None,
    )
    service.current_plan_id = plan.plan_id
    lanes = service._create_lanes(plan)
    assert plan.btc.quantity > plan.btc.amount_step
    partial_btc = plan.btc.quantity - plan.btc.amount_step
    assert partial_btc > plan.btc.amount_step / 2
    venues["BTC"].position = float(partial_btc)
    venues["ETH"].position = -float(plan.eth.quantity)

    hold_seconds = service._hold_open_pair(1, {}, lanes, plan.btc, plan.eth)

    assert hold_seconds == 0
    assert [event["event"] for event in events] == ["open_barrier_not_ready"]


def test_reverse_direction_hold_wait_uses_persisted_opening_sides(
    tmp_path,
    allocation: BetaAllocation,
) -> None:
    gateway = Gateway()
    plan = BetaVolumePlan.create(
        gateway,
        allocation,
        target_turnover_quote="200",
        round_turnover_quote="200",
        max_position_quote="1200",
        timeout_seconds=120,
        direction="btc_short_eth_long",
        now_ms=1000,
    )
    store = BetaVolumePlanStore(tmp_path)
    events: list[dict[str, object]] = []
    venues: dict[str, ImmediateVenue] = {}

    def venue_factory(unused_gateway: Gateway, symbol: str, position_side: str) -> ImmediateVenue:
        venues.setdefault(symbol, ImmediateVenue(symbol, position_side))
        return venues[symbol]

    service = LiveBetaVolumeService(
        gateway,
        Provider(allocation),  # type: ignore[arg-type]
        store,
        venue_factory=venue_factory,  # type: ignore[arg-type]
        gateway_factory=Gateway,
        event_sink=events.append,  # type: ignore[arg-type]
        hold_delay_seconds=lambda round_number: 3,
        now_ms=lambda: 1000,
        sleep=lambda seconds: None,
    )
    service.current_plan_id = plan.plan_id
    lanes = service._create_lanes(plan)
    venues["BTC"].position = -float(plan.btc.quantity)
    venues["ETH"].position = float(plan.eth.quantity)

    hold_seconds = service._hold_open_pair(1, {}, lanes, plan.btc, plan.eth)

    assert hold_seconds == 3
    assert [event["event"] for event in events] == [
        "open_barrier_verified",
        "hold_started",
        "hold_completed",
    ]


def test_close_pacing_rereads_beta_market_orders_and_positions(
    tmp_path,
    allocation: BetaAllocation,
) -> None:
    class CountingProvider(Provider):
        def __init__(self, selected: BetaAllocation) -> None:
            super().__init__(selected)
            self.calls = 0

        def get(self) -> BetaAllocation:
            self.calls += 1
            return super().get()

    class CountingGateway(Gateway):
        def __init__(self) -> None:
            super().__init__()
            self.book_calls = 0
            self.regular_order_calls = 0
            self.trigger_order_calls = 0

        def order_book(self, symbol: str, limit: int = 5) -> dict:
            self.book_calls += 1
            return super().order_book(symbol, limit)

        def open_orders(self, symbol: str | None = None, *, mode: str = "live", trigger: bool = False) -> list[dict]:
            self.regular_order_calls += 1
            return super().open_orders(symbol, mode=mode, trigger=trigger)

        def algo_orders(self, symbol: str | None = None) -> list[dict]:
            self.trigger_order_calls += 1
            return super().algo_orders(symbol)

    class CountingVenue(ImmediateVenue):
        def __init__(self, symbol: str, position_side: str) -> None:
            super().__init__(symbol, position_side)
            self.position_calls = 0

        def position_quantity(self) -> float:
            self.position_calls += 1
            return super().position_quantity()

    gateway = Gateway()
    plan = BetaVolumePlan.create(
        gateway,
        allocation,
        target_turnover_quote="200",
        round_turnover_quote="200",
        max_position_quote="1200",
        timeout_seconds=120,
        now_ms=1000,
    )
    store = BetaVolumePlanStore(tmp_path)
    store.save(plan)
    provider = CountingProvider(allocation)
    lanes = {"BTC": CountingGateway(), "ETH": CountingGateway()}
    venues: dict[str, CountingVenue] = {}
    before_close: dict[str, object] = {}

    def venue_factory(unused_gateway: Gateway, symbol: str, position_side: str) -> CountingVenue:
        venue = CountingVenue(symbol, position_side)
        venues[symbol] = venue
        return venue

    def phase_waiter(unused_plan_id: str, phase: str, unused_round: int) -> bool:
        if phase == "close":
            before_close["provider"] = provider.calls
            before_close["lanes"] = {
                symbol: (lane.book_calls, lane.regular_order_calls, lane.trigger_order_calls)
                for symbol, lane in lanes.items()
            }
            before_close["positions"] = {symbol: venue.position_calls for symbol, venue in venues.items()}
        return True

    result = LiveBetaVolumeService(
        gateway,
        provider,  # type: ignore[arg-type]
        store,
        venue_factory=venue_factory,  # type: ignore[arg-type]
        lane_gateways=lanes,  # type: ignore[arg-type]
        reconciler_factory=lambda unused: DeterministicReconciler(),
        phase_waiter=phase_waiter,
        now_ms=lambda: 1000,
        sleep=lambda _seconds: None,
    ).execute(plan)

    assert result["status"] == "completed"
    assert provider.calls > int(before_close["provider"])
    lane_counts = before_close["lanes"]
    position_counts = before_close["positions"]
    assert isinstance(lane_counts, dict) and isinstance(position_counts, dict)
    for symbol, lane in lanes.items():
        book, regular, trigger = lane_counts[symbol]
        assert lane.book_calls > book
        assert lane.regular_order_calls > regular
        assert lane.trigger_order_calls > trigger
        assert venues[symbol].position_calls > position_counts[symbol]


def test_stop_during_hold_cancels_both_lanes_then_maker_flattens_before_stopping(
    tmp_path,
    allocation: BetaAllocation,
) -> None:
    gateway = Gateway()
    plan = BetaVolumePlan.create(
        gateway,
        allocation,
        target_turnover_quote="400",
        round_turnover_quote="200",
        max_position_quote="1200",
        timeout_seconds=120,
        now_ms=1000,
    )
    store = BetaVolumePlanStore(tmp_path)
    store.save(plan)
    venues: dict[str, SafeStopVenue] = {}
    stop = threading.Event()
    events: list[dict[str, object]] = []

    def venue_factory(unused_gateway: Gateway, symbol: str, position_side: str) -> SafeStopVenue:
        venues.setdefault(symbol, SafeStopVenue(symbol, position_side))
        return venues[symbol]

    def event_sink(event: dict[str, object]) -> None:
        events.append(dict(event))
        if event.get("event") == "hold_started":
            stop.set()

    result = LiveBetaVolumeService(
        gateway,
        Provider(allocation),  # type: ignore[arg-type]
        store,
        venue_factory=venue_factory,  # type: ignore[arg-type]
        gateway_factory=Gateway,
        reconciler_factory=lambda unused_gateway: DeterministicReconciler(),
        now_ms=lambda: 1000,
        sleep=lambda seconds: None,
        hold_delay_seconds=lambda round_number: 30,
        stop_requested=stop.is_set,
        event_sink=event_sink,  # type: ignore[arg-type]
    ).execute(plan)

    assert result["status"] == "stopped"
    assert result["reason"] == "safe_stop_flattened"
    assert venues["BTC"].cancel_all_calls == 1
    assert venues["ETH"].cancel_all_calls == 1
    assert venues["BTC"].position == 0
    assert venues["ETH"].position == 0
    assert any(event["event"] == "safe_stop_started" for event in events)
    assert any(event["event"] == "safe_stop_verified" for event in events)


def test_close_barrier_retries_transient_position_timeout_and_still_flattens(
    tmp_path,
    allocation: BetaAllocation,
) -> None:
    gateway = Gateway()
    plan = BetaVolumePlan.create(
        gateway,
        allocation,
        target_turnover_quote="200",
        max_position_quote="1200",
        timeout_seconds=120,
        now_ms=1000,
    )
    store = BetaVolumePlanStore(tmp_path)
    store.save(plan)
    venues: dict[str, OneShotPositionTimeoutVenue] = {}

    def venue_factory(unused_gateway: Gateway, symbol: str, position_side: str) -> OneShotPositionTimeoutVenue:
        venues.setdefault(symbol, OneShotPositionTimeoutVenue(symbol, position_side))
        return venues[symbol]

    def event_sink(event: dict) -> None:
        if event.get("event") == "hold_completed":
            venues["BTC"].fail_next_position_read = True

    result = LiveBetaVolumeService(
        gateway,
        Provider(allocation),  # type: ignore[arg-type]
        store,
        venue_factory=venue_factory,  # type: ignore[arg-type]
        gateway_factory=Gateway,
        reconciler_factory=lambda unused_gateway: DeterministicReconciler(),
        now_ms=lambda: 1000,
        sleep=lambda seconds: None,
        hold_delay_seconds=lambda round_number: 1,
        event_sink=event_sink,
    ).execute(plan)

    assert result["status"] == "completed"
    assert venues["BTC"].position == pytest.approx(0)
    assert venues["ETH"].position == pytest.approx(0)
    assert any(
        row["event"] == "leg_waiting"
        and row.get("waiting_for") == "position_observation_retry"
        and row.get("symbol") == "BTC"
        for row in result["timeline"]
    )


def test_target_5000_runs_ten_flat_beta_cycles_with_authoritative_volume(tmp_path, allocation: BetaAllocation) -> None:
    gateway = Gateway()
    plan = BetaVolumePlan.create(
        gateway,
        allocation,
        target_turnover_quote="5000",
        round_turnover_quote="500",
        max_position_quote="1200",
        timeout_seconds=120,
        now_ms=1000,
    )
    store = BetaVolumePlanStore(tmp_path)
    store.save(plan)
    venues: dict[str, ImmediateVenue] = {}
    events: list[dict[str, object]] = []

    def venue_factory(unused_gateway: Gateway, symbol: str, position_side: str) -> ImmediateVenue:
        venues.setdefault(symbol, ImmediateVenue(symbol, position_side))
        return venues[symbol]

    result = LiveBetaVolumeService(
        gateway,
        Provider(allocation),  # type: ignore[arg-type]
        store,
        venue_factory=venue_factory,  # type: ignore[arg-type]
        gateway_factory=Gateway,
        reconciler_factory=lambda unused_gateway: DeterministicReconciler(),
        now_ms=lambda: 1000,
        sleep=lambda seconds: None,
        event_sink=lambda event: events.append(dict(event)),
    ).execute(plan)

    assert result["status"] == "completed"
    assert len(result["cycles"]) == 10
    assert Decimal(result["executed_quote_volume"]) >= Decimal("5000")
    assert result["accounting"]["fill_count"] == 40
    assert all(cycle["flat"] is True for cycle in result["cycles"])
    assert venues["BTC"].position == pytest.approx(0)
    assert venues["ETH"].position == pytest.approx(0)
    assert any(row["event"] == "pair_waiting" and row.get("action") == "open" for row in events)
    assert any(row["event"] == "leg_progress" and row.get("progress_event") == "submit" for row in events)
    assert any(row["event"] == "leg_waiting" and row.get("waiting_for") == "fill_reconciliation" for row in events)
    assert any(row["event"] == "final_acceptance_started" for row in events)
    assert any(row["event"] == "final_acceptance_completed" for row in events)


def test_auto_leverage_is_recomputed_and_verified_for_every_cycle(tmp_path, allocation: BetaAllocation) -> None:
    gateway = BalanceSequenceGateway(["1000", "60", "30"])
    plan = BetaVolumePlan.create(
        gateway,
        allocation,
        target_turnover_quote="400",
        round_turnover_quote="200",
        max_position_quote="1200",
        timeout_seconds=120,
        now_ms=1000,
    )
    store = BetaVolumePlanStore(tmp_path)
    store.save(plan)
    lane_gateways: list[Gateway] = []

    def gateway_factory() -> Gateway:
        lane = Gateway()
        lane_gateways.append(lane)
        return lane

    result = LiveBetaVolumeService(
        gateway,
        Provider(allocation),  # type: ignore[arg-type]
        store,
        venue_factory=lambda unused, symbol, side: ImmediateVenue(symbol, side),  # type: ignore[arg-type]
        gateway_factory=gateway_factory,  # type: ignore[arg-type]
        reconciler_factory=lambda unused: DeterministicReconciler(),
        now_ms=lambda: 1000,
        sleep=lambda seconds: None,
    ).execute(plan)

    assert result["status"] == "completed"
    assert [cycle["leverage"] for cycle in result["cycles"]] == [2, 4]
    assert [(update[0], update[1]) for update in gateway.leverage_updates] == [
        ("BTC", 2),
        ("ETH", 2),
        ("BTC", 4),
        ("ETH", 4),
    ]
    assert gateway.margin_mode_updates == [("BTC", "isolated"), ("ETH", "isolated")]
    assert all(not lane.leverage_updates for lane in lane_gateways)


def test_denied_leverage_stops_before_any_order_submission(tmp_path, allocation: BetaAllocation) -> None:
    gateway = DeniedBalanceGateway(["1000", "60"])
    plan = BetaVolumePlan.create(
        gateway,
        allocation,
        target_turnover_quote="200",
        max_position_quote="1200",
        timeout_seconds=120,
        now_ms=1000,
    )
    store = BetaVolumePlanStore(tmp_path)
    store.save(plan)
    venues: list[ImmediateVenue] = []

    def venue_factory(unused, symbol: str, side: str) -> ImmediateVenue:
        venue = ImmediateVenue(symbol, side)
        venues.append(venue)
        return venue

    result = LiveBetaVolumeService(
        gateway,
        Provider(allocation),  # type: ignore[arg-type]
        store,
        venue_factory=venue_factory,  # type: ignore[arg-type]
        gateway_factory=Gateway,
        reconciler_factory=lambda unused: DeterministicReconciler(),
        now_ms=lambda: 1000,
    ).execute(plan)

    assert result["status"] == "stopped"
    assert result["reason"] == "btc_leverage_update_permissiondenied"
    assert result["legs"] == []
    assert all(venue.order is None for venue in venues)


@pytest.mark.parametrize(
    ("failure", "reason", "expected_margin_calls", "expected_leverage_calls"),
    [
        ("margin_denied", "btc_margin_mode_update_permissiondenied", 1, 0),
        ("margin_mismatch", "btc_margin_mode_verify_mismatch", 1, 0),
        ("leverage_mismatch", "btc_leverage_verify_mismatch", 1, 1),
    ],
)
def test_cross_configuration_failure_stops_before_order_submission(
    tmp_path,
    allocation: BetaAllocation,
    failure: str,
    reason: str,
    expected_margin_calls: int,
    expected_leverage_calls: int,
) -> None:
    gateway = ConfigurationFailureGateway(failure)
    plan = BetaVolumePlan.create(
        Gateway(),
        allocation,
        target_turnover_quote="200",
        round_turnover_quote="200",
        max_position_quote="1200",
        timeout_seconds=120,
        leverage=400,
        margin_mode="cross",
        now_ms=1000,
    )
    store = BetaVolumePlanStore(tmp_path)
    store.save(plan)
    venues: list[ImmediateVenue] = []

    def venue_factory(unused, symbol: str, side: str) -> ImmediateVenue:
        venue = ImmediateVenue(symbol, side)
        venues.append(venue)
        return venue

    result = LiveBetaVolumeService(
        gateway,
        Provider(allocation),  # type: ignore[arg-type]
        store,
        venue_factory=venue_factory,  # type: ignore[arg-type]
        gateway_factory=Gateway,
        reconciler_factory=lambda unused: DeterministicReconciler(),
        now_ms=lambda: 1000,
        sleep=lambda _seconds: None,
    ).execute(plan)

    assert result["status"] == "stopped"
    assert result["reason"] == reason
    assert len(gateway.margin_mode_updates) == expected_margin_calls
    assert len(gateway.leverage_updates) == expected_leverage_calls
    assert result["legs"] == []
    assert all(venue.order is None for venue in venues)


def test_ambiguous_cross_configuration_is_only_read_back_and_never_resubmitted(
    tmp_path,
    allocation: BetaAllocation,
) -> None:
    gateway = ConfigurationFailureGateway("ambiguous_applied")
    plan = BetaVolumePlan.create(
        Gateway(),
        allocation,
        target_turnover_quote="200",
        round_turnover_quote="200",
        max_position_quote="1200",
        timeout_seconds=120,
        leverage=400,
        margin_mode="cross",
        now_ms=1000,
    )
    store = BetaVolumePlanStore(tmp_path)
    store.save(plan)

    result = LiveBetaVolumeService(
        gateway,
        Provider(allocation),  # type: ignore[arg-type]
        store,
        venue_factory=lambda unused, symbol, side: ImmediateVenue(symbol, side),  # type: ignore[arg-type]
        gateway_factory=Gateway,
        reconciler_factory=lambda unused: DeterministicReconciler(),
        now_ms=lambda: 1000,
        sleep=lambda _seconds: None,
    ).execute(plan)

    assert result["status"] == "completed"
    assert gateway.margin_mode_updates == [("BTC", "cross"), ("ETH", "cross")]
    assert gateway.leverage_updates == [("BTC", 400, "cross"), ("ETH", 400, "cross")]


def test_separately_claimed_recovery_flattens_observed_position_with_verified_maker_fill(
    tmp_path, allocation: BetaAllocation
) -> None:
    gateway = Gateway()
    gateway.positions_by_symbol["BTC"] = [{"side": "long", "contracts": "0.5"}]
    plan = BetaVolumePlan.create(
        gateway,
        allocation,
        target_turnover_quote="200",
        max_position_quote="1200",
        timeout_seconds=120,
        now_ms=1000,
    )
    store = BetaVolumePlanStore(tmp_path)
    store.save(plan, state="uncertain", result={"reason": "fills_not_visible"})
    venue = ImmediateVenue("BTC", "long")
    venue.position = 0.5

    store.claim_for_recovery(plan, "BTC")
    result = LiveBetaVolumeService(
        gateway,
        None,
        store,
        venue_factory=lambda unused, symbol, side: venue,  # type: ignore[arg-type]
        reconciler_factory=lambda unused: DeterministicReconciler(),
        now_ms=lambda: 1000,
    ).recover(plan, "BTC", Decimal("0.5"))

    assert result["status"] == "completed"
    assert result["maker_only"] is True
    assert result["final_position"] == pytest.approx(0)
    assert result["executed_quote_volume"] == "50"
    assert (tmp_path / f"{plan.plan_id}.btc.recovery.json").exists()
    store.claim_for_recovery(plan, "ETH")
    assert (tmp_path / f"{plan.plan_id}.eth.recovery.claim").exists()
    with pytest.raises(SafetyError, match="already claimed"):
        store.claim_for_recovery(plan, "BTC")


def test_legacy_plan_is_read_only_and_not_claimed(tmp_path, allocation: BetaAllocation) -> None:
    current = BetaVolumePlan.create(
        Gateway(),
        allocation,
        target_turnover_quote="200",
        max_position_quote="1200",
        timeout_seconds=120,
        now_ms=1000,
    )
    legacy = replace(current, schema_version=2)
    store = BetaVolumePlanStore(tmp_path)
    store.save(legacy)

    with pytest.raises(SafetyError, match="legacy Beta plans are read-only"):
        LiveBetaVolumeService(Gateway(), Provider(allocation), store, now_ms=lambda: 1000).execute(legacy)  # type: ignore[arg-type]

    assert store.load(legacy.plan_id)[1] == "planned"


def test_authoritative_fills_replace_sparse_executor_accounting(tmp_path, allocation: BetaAllocation) -> None:
    gateway = Gateway()
    plan = BetaVolumePlan.create(
        gateway,
        allocation,
        target_turnover_quote="200",
        max_position_quote="1200",
        timeout_seconds=120,
        now_ms=1000,
    )
    store = BetaVolumePlanStore(tmp_path)
    store.save(plan)
    venues: dict[str, SparseTerminalVenue] = {}

    def factory(unused_gateway: Gateway, symbol: str, position_side: str) -> SparseTerminalVenue:
        venues.setdefault(symbol, SparseTerminalVenue(symbol, position_side))
        return venues[symbol]

    result = LiveBetaVolumeService(
        gateway,
        Provider(allocation),  # type: ignore[arg-type]
        store,
        venue_factory=factory,  # type: ignore[arg-type]
        now_ms=lambda: 1000,
        gateway_factory=Gateway,
        reconciler_factory=lambda unused_gateway: DeterministicReconciler(),
        sleep=lambda seconds: None,
    ).execute(plan)

    assert result["status"] == "completed"
    assert result["executed_quote_volume"] == "200"
    assert result["accounting"]["fill_count"] == 4
    assert all(leg["executor_observation"]["fill_count"] == 0 for leg in result["legs"])
    assert all(leg["verification_status"] == "verified" for leg in result["legs"])


def test_missing_executor_order_identity_recovers_from_client_prefix_history(
    monkeypatch, tmp_path, allocation: BetaAllocation
) -> None:
    gateway = Gateway()
    plan = BetaVolumePlan.create(
        gateway,
        allocation,
        target_turnover_quote="200",
        max_position_quote="1200",
        timeout_seconds=120,
        now_ms=1000,
    )
    store = BetaVolumePlanStore(tmp_path)
    store.save(plan)
    venues: dict[str, ImmediateVenue] = {}

    def venue_factory(unused_gateway: Gateway, symbol: str, position_side: str) -> ImmediateVenue:
        venues.setdefault(symbol, ImmediateVenue(symbol, position_side))
        return venues[symbol]

    def execute_target(venue: ImmediateVenue, unused_policy, request, *, progress_sink=None) -> TargetExecutionResult:
        start = venue.position
        venue.position = request.target_position
        return TargetExecutionResult(
            status="completed",
            reason="target_reached",
            elapsed_ms=10,
            start_position=start,
            final_position=venue.position,
            target_position=request.target_position,
            quote_volume=0,
            fill_count=0,
            submissions=1,
            cancels=0,
            venue_cancels=0,
            preflight_skips=0,
            observation_errors=0,
            cancel_verification_attempts=0,
            cancel_verification_errors=0,
            requotes=0,
            maker_only=True,
            post_only_rejections=0,
            events=(),
        )

    monkeypatch.setattr("weex_cli.beta_volume.execute_adaptive_maker_target", execute_target)
    result = LiveBetaVolumeService(
        gateway,
        Provider(allocation),  # type: ignore[arg-type]
        store,
        venue_factory=venue_factory,  # type: ignore[arg-type]
        gateway_factory=lambda: HistoryIdentityGateway(plan.plan_id),
        reconciler_factory=lambda unused_gateway: DeterministicReconciler(),
        now_ms=lambda: 1000,
        sleep=lambda seconds: None,
    ).execute(plan)

    assert result["status"] == "completed"
    assert result["accounting"]["verified"] is True
    assert result["accounting"]["maker_only"] is True
    assert all(leg["reason"] != "missing_order_identity" for leg in result["legs"])


def test_one_lane_rejection_flattens_the_other_lane_before_stopping(tmp_path, allocation: BetaAllocation) -> None:
    gateway = Gateway()
    plan = BetaVolumePlan.create(
        gateway,
        allocation,
        target_turnover_quote="200",
        max_position_quote="1200",
        timeout_seconds=120,
        now_ms=1000,
    )
    store = BetaVolumePlanStore(tmp_path)
    store.save(plan)
    venues: dict[str, ImmediateVenue] = {}

    def factory(unused_gateway: Gateway, symbol: str, position_side: str) -> ImmediateVenue:
        venue = RejectingVenue(symbol, position_side) if symbol == "ETH" else ImmediateVenue(symbol, position_side)
        venues.setdefault(symbol, venue)
        return venues[symbol]

    service = LiveBetaVolumeService(
        gateway,
        Provider(allocation),  # type: ignore[arg-type]
        store,
        venue_factory=factory,  # type: ignore[arg-type]
        now_ms=lambda: 1000,
        gateway_factory=Gateway,
        reconciler_factory=lambda unused_gateway: DeterministicReconciler(),
        sleep=lambda seconds: None,
    )
    result = service.execute(plan)

    assert result["status"] == "stopped"
    assert result["reason"] == "post_only_rejected"
    assert len(result["legs"]) == 3
    assert venues["BTC"].position == pytest.approx(0)
    assert venues["ETH"].position == pytest.approx(0)
    assert store.load(plan.plan_id)[1] == "stopped"


def test_uncertain_lane_is_never_resubmitted_while_safe_lane_is_flattened(tmp_path, allocation: BetaAllocation) -> None:
    gateway = Gateway()
    plan = BetaVolumePlan.create(
        gateway,
        allocation,
        target_turnover_quote="200",
        max_position_quote="1200",
        timeout_seconds=120,
        now_ms=1000,
    )
    store = BetaVolumePlanStore(tmp_path)
    store.save(plan)
    venues: dict[str, ImmediateVenue] = {}

    def factory(unused_gateway: Gateway, symbol: str, position_side: str) -> ImmediateVenue:
        venue = UncertainVenue(symbol, position_side) if symbol == "ETH" else ImmediateVenue(symbol, position_side)
        venues.setdefault(symbol, venue)
        return venues[symbol]

    result = LiveBetaVolumeService(
        gateway,
        Provider(allocation),  # type: ignore[arg-type]
        store,
        venue_factory=factory,  # type: ignore[arg-type]
        now_ms=lambda: 1000,
        gateway_factory=Gateway,
        reconciler_factory=lambda unused_gateway: DeterministicReconciler(),
        sleep=lambda seconds: None,
    ).execute(plan)

    assert result["status"] == "uncertain"
    assert result["reason"] == "leg_exception:connectionerror"
    assert venues["BTC"].position == pytest.approx(0)
    assert venues["ETH"].position == pytest.approx(0)
    assert [leg["symbol"] for leg in result["legs"]].count("ETH") == 1


def test_late_accounting_is_refreshed_after_flatten_and_next_cycle_continues(
    tmp_path, allocation: BetaAllocation
) -> None:
    gateway = Gateway()
    plan = BetaVolumePlan.create(
        gateway,
        allocation,
        target_turnover_quote="400",
        round_turnover_quote="200",
        max_position_quote="1200",
        timeout_seconds=120,
        now_ms=1000,
    )
    store = BetaVolumePlanStore(tmp_path)
    store.save(plan)
    venues: dict[str, ImmediateVenue] = {}
    reconcilers: list[DelayedBtcOpenReconciler] = []

    def venue_factory(unused_gateway: Gateway, symbol: str, position_side: str) -> ImmediateVenue:
        venues.setdefault(symbol, ImmediateVenue(symbol, position_side))
        return venues[symbol]

    def reconciler_factory(unused_gateway: Gateway) -> DelayedBtcOpenReconciler:
        reconciler = DelayedBtcOpenReconciler()
        reconcilers.append(reconciler)
        return reconciler

    result = LiveBetaVolumeService(
        gateway,
        Provider(allocation),  # type: ignore[arg-type]
        store,
        venue_factory=venue_factory,  # type: ignore[arg-type]
        gateway_factory=Gateway,
        reconciler_factory=reconciler_factory,
        now_ms=lambda: 1000,
        sleep=lambda seconds: None,
    ).execute(plan)

    assert result["status"] == "completed"
    assert len(result["cycles"]) == 2
    assert all(cycle["flat"] is True for cycle in result["cycles"])
    assert venues["BTC"].position == pytest.approx(0)
    assert venues["ETH"].position == pytest.approx(0)
    requests = [request for reconciler in reconcilers for request in reconciler.requests]
    assert [(request.symbol, request.action) for request in requests].count(("BTC", "open")) == 3
    assert [(request.symbol, request.action) for request in requests].count(("BTC", "close")) == 2
    assert all("_pending_fill_reconciliation" not in leg for leg in result["legs"])
    assert len({leg["sequence"] for leg in result["legs"]}) == len(result["legs"])


def test_post_flat_accounting_uses_multiple_read_only_attempts_before_continuing(
    tmp_path, allocation: BetaAllocation
) -> None:
    gateway = Gateway()
    plan = BetaVolumePlan.create(
        gateway,
        allocation,
        target_turnover_quote="200",
        max_position_quote="1200",
        timeout_seconds=120,
        now_ms=1000,
    )
    store = BetaVolumePlanStore(tmp_path)
    store.save(plan)
    venues: dict[str, ImmediateVenue] = {}
    reconcilers: list[TwiceDelayedBtcOpenReconciler] = []

    def venue_factory(unused_gateway: Gateway, symbol: str, position_side: str) -> ImmediateVenue:
        venues.setdefault(symbol, ImmediateVenue(symbol, position_side))
        return venues[symbol]

    def reconciler_factory(unused_gateway: Gateway) -> TwiceDelayedBtcOpenReconciler:
        reconciler = TwiceDelayedBtcOpenReconciler()
        reconcilers.append(reconciler)
        return reconciler

    result = LiveBetaVolumeService(
        gateway,
        Provider(allocation),  # type: ignore[arg-type]
        store,
        venue_factory=venue_factory,  # type: ignore[arg-type]
        gateway_factory=Gateway,
        reconciler_factory=reconciler_factory,
        now_ms=lambda: 1000,
        sleep=lambda seconds: None,
    ).execute(plan)

    assert result["status"] == "completed"
    assert venues["BTC"].position == pytest.approx(0)
    btc_open = next(leg for leg in result["legs"] if leg["symbol"] == "BTC" and leg["action"] == "open")
    assert btc_open["post_flat_reconciliation_attempts"] == 2
    assert sum(reconciler.open_calls for reconciler in reconcilers) == 3


@pytest.mark.parametrize(
    ("reconciler_factory", "expected_status", "expected_reason"),
    [
        (InvisibleBtcOpenReconciler, "uncertain", "fills_not_visible"),
        (DelayedTakerBtcOpenReconciler, "stopped", "taker_fill_detected"),
    ],
)
def test_unresolved_or_taker_late_accounting_stops_flat(
    tmp_path,
    allocation: BetaAllocation,
    reconciler_factory,
    expected_status: str,
    expected_reason: str,
) -> None:
    gateway = Gateway()
    plan = BetaVolumePlan.create(
        gateway,
        allocation,
        target_turnover_quote="200",
        max_position_quote="1200",
        timeout_seconds=120,
        now_ms=1000,
    )
    store = BetaVolumePlanStore(tmp_path)
    store.save(plan)
    venues: dict[str, ImmediateVenue] = {}

    def venue_factory(unused_gateway: Gateway, symbol: str, position_side: str) -> ImmediateVenue:
        venues.setdefault(symbol, ImmediateVenue(symbol, position_side))
        return venues[symbol]

    result = LiveBetaVolumeService(
        gateway,
        Provider(allocation),  # type: ignore[arg-type]
        store,
        venue_factory=venue_factory,  # type: ignore[arg-type]
        gateway_factory=Gateway,
        reconciler_factory=lambda unused_gateway: reconciler_factory(),
        now_ms=lambda: 1000,
        sleep=lambda seconds: None,
    ).execute(plan)

    assert result["status"] == expected_status
    assert result["reason"] == expected_reason
    assert result["cycles"][0]["flat"] is True
    assert venues["BTC"].position == pytest.approx(0)
    assert venues["ETH"].position == pytest.approx(0)
    assert all("_pending_fill_reconciliation" not in leg for leg in result["legs"])


def test_taker_fill_is_terminal_after_both_observable_lanes_are_flattened(tmp_path, allocation: BetaAllocation) -> None:
    gateway = Gateway()
    plan = BetaVolumePlan.create(
        gateway,
        allocation,
        target_turnover_quote="200",
        max_position_quote="1200",
        timeout_seconds=120,
        now_ms=1000,
    )
    store = BetaVolumePlanStore(tmp_path)
    store.save(plan)
    venues: dict[str, ImmediateVenue] = {}

    def factory(unused_gateway: Gateway, symbol: str, position_side: str) -> ImmediateVenue:
        venues.setdefault(symbol, ImmediateVenue(symbol, position_side))
        return venues[symbol]

    result = LiveBetaVolumeService(
        gateway,
        Provider(allocation),  # type: ignore[arg-type]
        store,
        venue_factory=factory,  # type: ignore[arg-type]
        now_ms=lambda: 1000,
        gateway_factory=Gateway,
        reconciler_factory=lambda unused_gateway: TakerOnEthOpenReconciler(),
        sleep=lambda seconds: None,
    ).execute(plan)

    assert result["status"] == "stopped"
    assert result["reason"] == "taker_fill_detected"
    assert result["accounting"]["taker_count"] == 1
    assert result["maker_only"] is False
    assert venues["BTC"].position == pytest.approx(0)
    assert venues["ETH"].position == pytest.approx(0)


def test_partial_open_is_reconciled_flat_then_next_cycle_continues(
    monkeypatch, tmp_path, allocation: BetaAllocation
) -> None:
    gateway = Gateway()
    plan = BetaVolumePlan.create(
        gateway,
        allocation,
        target_turnover_quote="200",
        max_position_quote="1200",
        timeout_seconds=120,
        now_ms=1000,
    )
    store = BetaVolumePlanStore(tmp_path)
    store.save(plan)
    venues: dict[str, ImmediateVenue] = {}
    partial_done = False

    def factory(unused_gateway: Gateway, symbol: str, position_side: str) -> ImmediateVenue:
        venues.setdefault(symbol, ImmediateVenue(symbol, position_side))
        return venues[symbol]

    def execute_target(venue: ImmediateVenue, unused_policy, request, *, progress_sink=None) -> TargetExecutionResult:
        nonlocal partial_done
        start = venue.position
        partial = request.target_position != 0 and venue.symbol == "BTC" and not partial_done
        if partial:
            partial_done = True
            venue.position = request.target_position / 2
        else:
            venue.position = request.target_position
        executed = abs(venue.position - start)
        status = "failed" if partial else "completed"
        reason = "deadline_exceeded" if partial else "target_reached"
        return TargetExecutionResult(
            status=status,
            reason=reason,
            elapsed_ms=10,
            start_position=start,
            final_position=venue.position,
            target_position=request.target_position,
            quote_volume=0,
            fill_count=0,
            submissions=1,
            cancels=1 if partial else 0,
            venue_cancels=0,
            preflight_skips=0,
            observation_errors=0,
            cancel_verification_attempts=1 if partial else 0,
            cancel_verification_errors=0,
            requotes=0,
            maker_only=True,
            post_only_rejections=0,
            events=({"event": "submit", "order_id": request.client_prefix},) if executed else (),
        )

    monkeypatch.setattr("weex_cli.beta_volume.execute_adaptive_maker_target", execute_target)
    result = LiveBetaVolumeService(
        gateway,
        Provider(allocation),  # type: ignore[arg-type]
        store,
        venue_factory=factory,  # type: ignore[arg-type]
        now_ms=lambda: 1000,
        gateway_factory=Gateway,
        reconciler_factory=lambda unused_gateway: DeterministicReconciler(),
        sleep=lambda seconds: None,
    ).execute(plan)

    assert result["status"] == "completed"
    assert result["cycles"][0]["status"] == "recovered"
    assert len(result["cycles"]) == 2
    assert Decimal(result["executed_quote_volume"]) >= Decimal("200")
    assert venues["BTC"].position == pytest.approx(0)
    assert venues["ETH"].position == pytest.approx(0)


def test_preflight_accepts_beta_changes_but_rejects_existing_exposure(
    tmp_path, allocation: BetaAllocation
) -> None:
    gateway = Gateway()
    plan = BetaVolumePlan.create(
        gateway,
        allocation,
        target_turnover_quote="200",
        max_position_quote="1200",
        timeout_seconds=120,
        now_ms=1000,
    )
    moved = BetaAllocation(
        beta=Decimal("1.1"),
        btc_long_weight=Decimal(1) / Decimal("2.1"),
        eth_short_weight=Decimal("1.1") / Decimal("2.1"),
        version="beta-v1:124",
        as_of_ms=124,
        confidence=Decimal("0.8"),
        confidence_threshold=Decimal("0.65"),
        source="test",
    )
    service = LiveBetaVolumeService(
        gateway,
        Provider(moved),
        BetaVolumePlanStore(tmp_path),
        now_ms=lambda: 1000,  # type: ignore[arg-type]
    )
    preflight = service.preflight(plan)
    assert preflight["fresh_beta_version"] == moved.version
    assert "beta_drift" not in preflight

    gateway.positions_by_symbol["BTC"] = [{"side": "long", "contracts": "0.1"}]
    service = LiveBetaVolumeService(
        gateway,
        Provider(allocation),
        BetaVolumePlanStore(tmp_path),
        now_ms=lambda: 1000,  # type: ignore[arg-type]
    )
    with pytest.raises(SafetyError, match="positions or orders"):
        service.preflight(plan)
