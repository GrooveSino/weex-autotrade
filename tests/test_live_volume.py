from __future__ import annotations

from collections.abc import Callable
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Any

import pytest

from weex_cli.adaptive_executor import TargetExecutionResult, TargetRequest
from weex_cli.adaptive_maker import MarketSnapshot
from weex_cli.errors import SafetyError
from weex_cli.execution_reconciliation import LegFillReport, LegFillRequest
from weex_cli.live_volume import (
    LiveMakerVolumePlan,
    LiveMakerVolumePlanStore,
    LiveMakerVolumeService,
    live_maker_volume_confirmation,
)


class Gateway:
    def __init__(self) -> None:
        self.positions_by_side: dict[str, float] = {"long": 0.0, "short": 0.0}
        self.active_orders: list[dict[str, Any]] = []
        self.trigger_orders: list[dict[str, Any]] = []
        self.available = Decimal("10000")

    def order_book(self, symbol: str, limit: int = 5) -> dict[str, Any]:
        return {"bids": [[99, 10], [98, 10]], "asks": [[101, 10], [102, 10]]}

    def amount_step(self, symbol: str) -> Decimal:
        return Decimal("0.1")

    def amount_to_precision(self, symbol: str, value: Decimal) -> Decimal:
        return value.quantize(Decimal("0.1"), rounding=ROUND_DOWN)

    def account_balance_rows(self, mode: str) -> list[dict[str, Any]]:
        return [{"asset": "USDT", "availableBalance": str(self.available)}]

    def positions(self, mode: str, symbol: str | None = None) -> list[dict[str, Any]]:
        rows = []
        for side, signed in self.positions_by_side.items():
            if abs(signed) > 0:
                rows.append({"side": side, "contracts": str(abs(signed)), "info": {"positionSide": side}})
        return rows

    def open_orders(self, symbol: str | None = None, mode: str = "live") -> list[dict[str, Any]]:
        return list(self.active_orders)

    def algo_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        return list(self.trigger_orders)


class Venue:
    def __init__(self, gateway: Gateway, position_side: str) -> None:
        self.gateway = gateway
        self.position_side = position_side

    @property
    def now_ms(self) -> int:
        return 1_000

    def snapshot(self) -> MarketSnapshot:
        return MarketSnapshot(1_000, 99, 101, 10, 10, 5, 5, 1, 1)

    def position_quantity(self) -> float:
        return self.gateway.positions_by_side[self.position_side]


class Reconciler:
    def __init__(self, *, taker_actions: set[str] | None = None) -> None:
        self.requests: list[LegFillRequest] = []
        self.taker_actions = taker_actions or set()

    def reconcile(self, request: LegFillRequest) -> LegFillReport:
        self.requests.append(request)
        taker = request.action in self.taker_actions
        return LegFillReport(
            status="taker_fill_detected" if taker else "verified",
            source_complete=True,
            fill_count=1,
            order_count=1,
            executed_quantity=request.expected_quantity,
            quote_volume=request.expected_quantity * Decimal("100"),
            maker_only=not taker,
            maker_count=0 if taker else 1,
            taker_count=1 if taker else 0,
            unknown_liquidity_count=0,
            commission_by_asset={"USDT": Decimal("0.01")},
            realized_pnl=Decimal("-0.01") if request.action == "close" else Decimal(0),
        )


class MismatchReconciler(Reconciler):
    def reconcile(self, request: LegFillRequest) -> LegFillReport:
        report = super().reconcile(request)
        if request.action != "open":
            return report
        return LegFillReport(
            status="quantity_mismatch",
            source_complete=True,
            fill_count=report.fill_count,
            order_count=report.order_count,
            executed_quantity=report.executed_quantity + Decimal("0.1"),
            quote_volume=report.quote_volume,
            maker_only=True,
            maker_count=report.maker_count,
            taker_count=0,
            unknown_liquidity_count=0,
            commission_by_asset=report.commission_by_asset,
            realized_pnl=report.realized_pnl,
        )


class Executor:
    def __init__(self, behavior: Callable[[int, Venue, TargetRequest], tuple[str, str, float]] | None = None) -> None:
        self.calls: list[TargetRequest] = []
        self.behavior = behavior

    def __call__(self, venue: Venue, policy: object, request: TargetRequest) -> TargetExecutionResult:
        index = len(self.calls) + 1
        self.calls.append(request)
        start = venue.position_quantity()
        status, reason, final = (
            self.behavior(index, venue, request)
            if self.behavior is not None
            else ("completed", "target_reached", request.target_position)
        )
        venue.gateway.positions_by_side[venue.position_side] = final
        filled = abs(final - start)
        events = (
            ({"event": "submit", "order_id": f"order-{index}"},) if filled > 0 or reason == "post_only_rejected" else ()
        )
        return TargetExecutionResult(
            status=status,
            reason=reason,
            elapsed_ms=10,
            start_position=start,
            final_position=final,
            target_position=request.target_position,
            quote_volume=filled * 100,
            fill_count=1 if filled else 0,
            submissions=1,
            cancels=1 if status != "completed" else 0,
            venue_cancels=0,
            preflight_skips=0,
            observation_errors=0,
            cancel_verification_attempts=1,
            cancel_verification_errors=0,
            requotes=0,
            maker_only=True,
            post_only_rejections=1 if reason == "post_only_rejected" else 0,
            events=events,
        )


@pytest.fixture
def gateway() -> Gateway:
    return Gateway()


def make_plan(gateway: Gateway, **overrides: Any) -> LiveMakerVolumePlan:
    values: dict[str, Any] = {
        "symbol": "BTC",
        "target_quote": "5000",
        "round_quote": "500",
        "timeout_seconds": 120,
        "leverage": 2,
        "now_ms": 1_000,
    }
    values.update(overrides)
    return LiveMakerVolumePlan.create(gateway, **values)  # type: ignore[arg-type]


def service(
    gateway: Gateway,
    store: LiveMakerVolumePlanStore,
    executor: Executor,
    reconciler: Reconciler | None = None,
) -> LiveMakerVolumeService:
    return LiveMakerVolumeService(
        gateway,  # type: ignore[arg-type]
        store,
        venue_factory=lambda current, symbol, side: Venue(current, side),  # type: ignore[arg-type]
        fill_reconciler=reconciler or Reconciler(),
        executor=executor,  # type: ignore[arg-type]
        now_ms=lambda: 1_000,
        sleep=lambda _: None,
    )


def test_plan_defines_flat_round_exposure_and_exact_confirmation(gateway: Gateway) -> None:
    plan = make_plan(gateway)

    assert plan.estimated_rounds == 10
    assert plan.max_position_quote == Decimal("275.00")
    assert plan.required_available_quote == Decimal("165.000")
    assert live_maker_volume_confirmation(plan) == (
        "EXECUTE WEEX LIVE MAKER VOLUME BTC TARGET_5000 ROUND_500 LEVERAGE_2 "
        f"TIMEOUT_120 RECOVERY_3 EMPTY_3 POST_ONLY {plan.plan_id.upper()}"
    )


def test_service_runs_ten_alternating_rounds_to_verified_target(gateway: Gateway, tmp_path: Path) -> None:
    plan = make_plan(gateway)
    store = LiveMakerVolumePlanStore(tmp_path)
    store.create(plan)
    executor = Executor()
    reconciler = Reconciler()

    result = service(gateway, store, executor, reconciler).execute(plan)

    assert result["status"] == "completed"
    assert result["verified_quote"] == "5000"
    assert result["rounds_completed"] == 10
    assert result["maker_count"] == 20
    assert result["taker_count"] == 0
    assert [row["position_side"] for row in result["rounds"]] == ["long", "short"] * 5
    assert gateway.positions_by_side == {"long": 0.0, "short": 0.0}
    assert store.load_record(plan.plan_id).state == "completed"


def test_partial_open_is_maker_flattened_and_session_continues(gateway: Gateway, tmp_path: Path) -> None:
    def behavior(index: int, venue: Venue, request: TargetRequest) -> tuple[str, str, float]:
        if index == 1:
            return "failed", "deadline_exceeded", request.target_position / 2
        return "completed", "target_reached", request.target_position

    plan = make_plan(gateway, target_quote="500", round_quote="500")
    store = LiveMakerVolumePlanStore(tmp_path)
    store.create(plan)

    result = service(gateway, store, Executor(behavior)).execute(plan)

    assert result["status"] == "completed"
    assert Decimal(result["verified_quote"]) >= Decimal("500")
    assert result["excess_quote"] == "10"
    assert result["rounds"][0]["status"] == "recovered"
    assert result["rounds_completed"] == 3
    assert gateway.positions_by_side["long"] == 0


def test_partial_close_gets_a_second_confirmed_maker_attempt(gateway: Gateway, tmp_path: Path) -> None:
    def behavior(index: int, venue: Venue, request: TargetRequest) -> tuple[str, str, float]:
        if index == 2:
            return "failed", "deadline_exceeded", venue.position_quantity() / 2
        return "completed", "target_reached", request.target_position

    plan = make_plan(gateway, target_quote="500", round_quote="500")
    store = LiveMakerVolumePlanStore(tmp_path)
    store.create(plan)
    executor = Executor(behavior)

    result = service(gateway, store, executor).execute(plan)

    assert result["status"] == "completed"
    assert len(executor.calls) == 3
    assert result["verified_quote"] == "500"
    assert gateway.positions_by_side["long"] == 0


def test_post_only_rejection_is_terminal_without_resubmission(gateway: Gateway, tmp_path: Path) -> None:
    def reject(index: int, venue: Venue, request: TargetRequest) -> tuple[str, str, float]:
        return "failed", "post_only_rejected", venue.position_quantity()

    plan = make_plan(gateway, target_quote="500", round_quote="500")
    store = LiveMakerVolumePlanStore(tmp_path)
    store.create(plan)
    executor = Executor(reject)

    result = service(gateway, store, executor).execute(plan)

    assert result["status"] == "stopped"
    assert result["reason"] == "post_only_rejected"
    assert len(executor.calls) == 1
    assert result["verified_quote"] == "0"


def test_uncertain_order_state_stops_before_another_submission(gateway: Gateway, tmp_path: Path) -> None:
    def uncertain(index: int, venue: Venue, request: TargetRequest) -> tuple[str, str, float]:
        return "uncertain", "cancel_not_confirmed", venue.position_quantity()

    plan = make_plan(gateway, target_quote="500", round_quote="500")
    store = LiveMakerVolumePlanStore(tmp_path)
    store.create(plan)
    executor = Executor(uncertain)

    result = service(gateway, store, executor).execute(plan)

    assert result["status"] == "uncertain"
    assert result["reconciliation_required"] is True
    assert len(executor.calls) == 1


def test_accounting_uncertainty_flattens_before_stopping(gateway: Gateway, tmp_path: Path) -> None:
    plan = make_plan(gateway, target_quote="500", round_quote="500")
    store = LiveMakerVolumePlanStore(tmp_path)
    store.create(plan)
    executor = Executor()

    result = service(gateway, store, executor, MismatchReconciler()).execute(plan)

    assert result["status"] == "uncertain"
    assert result["reason"] == "quantity_mismatch"
    assert len(executor.calls) == 2
    assert gateway.positions_by_side["long"] == 0


def test_taker_fill_is_not_counted_and_position_is_still_flattened(gateway: Gateway, tmp_path: Path) -> None:
    plan = make_plan(gateway, target_quote="500", round_quote="500")
    store = LiveMakerVolumePlanStore(tmp_path)
    store.create(plan)
    reconciler = Reconciler(taker_actions={"open"})

    result = service(gateway, store, Executor(), reconciler).execute(plan)

    assert result["status"] == "stopped"
    assert result["reason"] == "taker_fill_detected"
    assert result["verified_quote"] == "250"
    assert result["taker_count"] == 1
    assert gateway.positions_by_side["long"] == 0


def test_preflight_refuses_existing_symbol_orders_before_execution(gateway: Gateway, tmp_path: Path) -> None:
    gateway.active_orders.append({"id": "existing"})
    plan = make_plan(gateway)
    store = LiveMakerVolumePlanStore(tmp_path)
    store.create(plan)
    executor = Executor()

    with pytest.raises(SafetyError, match="positions or orders"):
        service(gateway, store, executor).execute(plan)

    assert executor.calls == []
    assert store.load_record(plan.plan_id).state == "rejected"
