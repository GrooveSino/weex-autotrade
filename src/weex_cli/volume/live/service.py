"""Top-level orchestration for explicitly approved live Maker volume plans."""

from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Callable, Mapping
from decimal import Decimal
from typing import Any

from weex_cli.core.errors import SafetyError
from weex_cli.core.models import decimal_text
from weex_cli.exchange.rest.gateway import WeexGateway
from weex_cli.execution.adaptive import MakerVenue, TargetExecutionResult, TargetRequest, execute_adaptive_maker_target
from weex_cli.execution.adaptive_maker import MakerPolicy
from weex_cli.execution.reconciliation import LegFillReconciler, LiveLegFillReconciler
from weex_cli.execution.venues import LiveAdaptiveMakerVenue

from .contracts import PLAN_MAX_AGE_SECONDS, LiveMakerVolumePlan
from .legs import LiveMakerVolumeLegsMixin
from .lifecycle import LiveMakerVolumeLifecycleMixin
from .rounds import LiveMakerVolumeRoundsMixin
from .store import LiveMakerVolumePlanStore
from .support import active_positions, available_quote, mid_price, row_count

MAX_PLAN_PRICE_DRIFT = Decimal("0.05")
VenueFactory = Callable[[WeexGateway, str, str], LiveAdaptiveMakerVenue]
EventSink = Callable[[Mapping[str, Any]], None]
Executor = Callable[[MakerVenue, MakerPolicy, TargetRequest], TargetExecutionResult]


class LiveMakerVolumeService(
    LiveMakerVolumeRoundsMixin,
    LiveMakerVolumeLegsMixin,
    LiveMakerVolumeLifecycleMixin,
):
    def __init__(
        self,
        gateway: WeexGateway,
        store: LiveMakerVolumePlanStore,
        *,
        venue_factory: VenueFactory = LiveAdaptiveMakerVenue,
        fill_reconciler: LegFillReconciler | None = None,
        now_ms: Callable[[], int] = lambda: int(time.time() * 1000),
        sleep: Callable[[float], None] = time.sleep,
        event_sink: EventSink | None = None,
        executor: Executor = execute_adaptive_maker_target,
    ) -> None:
        self.gateway = gateway
        self.store = store
        self.venue_factory = venue_factory
        self.fill_reconciler = fill_reconciler or LiveLegFillReconciler(gateway, now_ms=now_ms)
        self.now_ms = now_ms
        self.sleep = sleep
        self.event_sink = event_sink
        self.executor = executor
        self.timeline: list[dict[str, Any]] = []
        self.plan: LiveMakerVolumePlan | None = None
        self.rounds: list[dict[str, Any]] = []
        self.verified_quote = Decimal(0)
        self.maker_count = 0
        self.taker_count = 0
        self.unknown_liquidity_count = 0
        self.fill_count = 0
        self.commission_by_asset: dict[str, Decimal] = defaultdict(Decimal)
        self.realized_pnl = Decimal(0)
        self.started_at_ms = 0

    def preflight(self, plan: LiveMakerVolumePlan) -> dict[str, Any]:
        if self.now_ms() - plan.created_at_ms > PLAN_MAX_AGE_SECONDS * 1000:
            raise SafetyError("live Maker volume plan expired; create and review a new dry run")
        current_price = mid_price(self.gateway, plan.symbol)
        drift = abs(current_price - plan.reference_price) / plan.reference_price
        if drift > MAX_PLAN_PRICE_DRIFT:
            raise SafetyError("market moved more than 5% since planning; create a new dry run")
        if self.gateway.amount_step(plan.symbol) != plan.amount_step:
            raise SafetyError("market amount precision changed since planning; create a new dry run")
        positions = active_positions(self.gateway, plan.symbol)
        regular_orders = self.gateway.open_orders(plan.symbol, mode="live")
        trigger_orders = row_count(self.gateway.algo_orders(plan.symbol))
        if positions or regular_orders or trigger_orders:
            raise SafetyError("symbol has positions or orders; refusing to start a new volume session")
        available = available_quote(self.gateway)
        if available < plan.required_available_quote:
            raise SafetyError("available USDT is insufficient for the declared leverage and round size")
        return {
            "available_sufficient": True,
            "declared_leverage": plan.leverage,
            "price_drift": decimal_text(drift),
            "active_position_count": 0,
            "regular_order_count": 0,
            "trigger_order_count": 0,
        }

    def execute(self, plan: LiveMakerVolumePlan) -> dict[str, Any]:
        self._reset(plan)
        self.store.claim_for_execution(plan)
        self._emit("volume_preflight_started", symbol=plan.symbol)
        try:
            preflight = self.preflight(plan)
        except Exception as exc:
            reason = f"preflight_exception:{type(exc).__name__.lower()}"
            self._emit("volume_preflight_rejected", reason=reason)
            payload = self._result("rejected", reason, reconciliation_required=False)
            self.store.save(plan, state="rejected", result=payload)
            raise
        self._emit("volume_preflight_completed", symbol=plan.symbol)
        self._checkpoint("executing", "preflight_completed", preflight=preflight)

        empty_rounds = 0
        round_number = 0
        max_completed_rounds = plan.estimated_rounds * 3 + plan.max_empty_rounds + 5
        while self.verified_quote < plan.target_quote:
            if sum(1 for row in self.rounds if row.get("status") in {"completed", "recovered"}) >= max_completed_rounds:
                return self._finish("stopped", "round_limit_exhausted")
            round_number += 1
            remaining = plan.target_quote - self.verified_quote
            desired_quote = min(plan.round_quote, remaining)
            outcome = self._execute_round(round_number, desired_quote)
            self.rounds.append(outcome)
            self._checkpoint("executing", "round_checkpointed")

            if outcome["status"] == "empty":
                empty_rounds += 1
                if empty_rounds > plan.max_empty_rounds:
                    return self._finish("stopped", "empty_round_limit_exhausted")
            else:
                empty_rounds = 0
            if outcome["terminal"]:
                status = "uncertain" if outcome["uncertain"] else "stopped"
                return self._finish(status, str(outcome["reason"]))
            if self.verified_quote < plan.target_quote and plan.cooldown_seconds:
                self._emit(
                    "volume_cooldown",
                    round=round_number,
                    seconds=plan.cooldown_seconds,
                    remaining_quote=decimal_text(plan.target_quote - self.verified_quote),
                )
                self.sleep(plan.cooldown_seconds)

        return self._final_acceptance()
