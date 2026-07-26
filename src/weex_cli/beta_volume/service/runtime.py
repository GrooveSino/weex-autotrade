from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from decimal import Decimal
from typing import Any

from weex_cli.beta_campaign.allocation import HttpBetaAllocationProvider
from weex_cli.core.errors import SafetyError
from weex_cli.core.reliability import NETWORK_ERRORS, retry_read
from weex_cli.exchange.rest.gateway import WeexGateway
from weex_cli.execution.reconciliation import (
    LiveLegFillReconciler,
)
from weex_cli.execution.venues import LiveAdaptiveMakerVenue

from ..contracts import (
    BETA_READ_RETRY_POLICY,
    MAX_PRICE_DRIFT,
    PLAN_MAX_AGE_SECONDS,
    POSITION_READ_RETRY_POLICY,
    DelaySelector,
    EventSink,
    ExecutionLane,
    GatewayFactory,
    PhaseWaiter,
    ReconcilerFactory,
    VenueFactory,
)
from ..plan import BetaVolumePlan
from ..safety import (
    inspect_live_account,
)
from ..sizing import _mid_price
from ..store import BetaVolumePlanStore


class RuntimeMixin:
    def __init__(
        self,
        gateway: WeexGateway,
        provider: HttpBetaAllocationProvider | None,
        store: BetaVolumePlanStore,
        *,
        venue_factory: VenueFactory | None = None,
        now_ms: Callable[[], int] = lambda: int(time.time() * 1000),
        gateway_factory: GatewayFactory | None = None,
        lane_gateways: Mapping[str, WeexGateway] | None = None,
        reconciler_factory: ReconcilerFactory | None = None,
        event_sink: EventSink | None = None,
        sleep: Callable[[float], None] = time.sleep,
        hold_delay_seconds: DelaySelector | None = None,
        round_gap_delay_seconds: DelaySelector | None = None,
        market_data: Any | None = None,
        order_updates: Any | None = None,
        stop_requested: Callable[[], bool] | None = None,
        phase_waiter: PhaseWaiter | None = None,
    ) -> None:
        self.gateway = gateway
        self.provider = provider
        self.store = store
        self.venue_factory = venue_factory
        self.now_ms = now_ms
        self.gateway_factory = gateway_factory or getattr(gateway, "fork", None)
        self.lane_gateways = dict(lane_gateways) if lane_gateways is not None else None
        self.reconciler_factory = reconciler_factory or (
            lambda lane_gateway: LiveLegFillReconciler(lane_gateway, now_ms=now_ms)
        )
        self.event_sink = event_sink
        self.sleep = sleep
        self.hold_delay_seconds = hold_delay_seconds
        self.round_gap_delay_seconds = round_gap_delay_seconds
        self.market_data = market_data
        self.order_updates = order_updates
        self.stop_requested = stop_requested or (lambda: False)
        self.phase_waiter = phase_waiter
        self._stop_callback_configured = stop_requested is not None
        self.timeline: list[dict[str, Any]] = []
        self.current_plan_id: str | None = None
        self._event_lock = threading.Lock()

    def preflight(self, plan: BetaVolumePlan) -> dict[str, Any]:
        if self.now_ms() - plan.created_at_ms > PLAN_MAX_AGE_SECONDS * 1000:
            raise SafetyError("Beta plan expired; create and review a new dry run")
        if self.provider is None:
            raise SafetyError("Beta provider is required for a normal execution")
        current = self.provider.get()
        for leg in (plan.btc, plan.eth):
            current_price = _mid_price(self.gateway, leg.symbol)
            drift = abs(current_price - leg.reference_price) / leg.reference_price
            if drift > MAX_PRICE_DRIFT:
                raise SafetyError(f"{leg.symbol} moved more than 1% since planning; create a new dry run")
        account = inspect_live_account(
            self.gateway,
            plan.required_available_quote,
            opening_notional=plan.estimated_turnover_quote / 2,
            leverage=plan.leverage,
            max_auto_leverage=plan.max_auto_leverage,
            margin_buffer=plan.margin_buffer,
        )
        if not account["available_sufficient"]:
            raise SafetyError("available USDT is insufficient for the planned opening budget")
        if account["active_position_count"] or account["regular_order_count"] or account["trigger_order_count"]:
            raise SafetyError("BTC/ETH positions or orders are present; refusing paired opening")
        return {**account, "fresh_beta_version": current.version}

    def execute(self, plan: BetaVolumePlan) -> dict[str, Any]:
        if plan.schema_version < 3:
            raise SafetyError("legacy Beta plans are read-only; create and review a new auto-leverage plan")
        self.timeline = []
        self.current_plan_id = plan.plan_id
        execution_started_ms = self.now_ms()
        self._emit("preflight_started", message="Checking Beta, prices, funds, positions, and orders")
        preflight = self._preflight_with_read_retry(plan)
        self.store.claim_for_execution(plan)
        self._emit("preflight_completed", message="Account is ready and flat")
        lanes = self._create_lanes(plan)
        try:
            if self.stop_requested():
                return self._safe_stop(
                    plan,
                    lanes,
                    preflight,
                    execution_started_ms,
                    summaries=[],
                    cycles=[],
                    total_quote=Decimal(0),
                    round_number=0,
                )
            return self._execute_cycles(plan, lanes, preflight, execution_started_ms)
        finally:
            if self.lane_gateways is None:
                for lane in lanes.values():
                    close = getattr(lane.gateway, "close", None)
                    if callable(close):
                        close()

    def _preflight_with_read_retry(self, plan: BetaVolumePlan) -> dict[str, Any]:
        try:
            return self._read_with_retry(
                lambda: self.preflight(plan),
                operation="preflight",
                retry_event="preflight_retry",
            )
        except NETWORK_ERRORS as exc:
            reason = f"preflight_exception:{type(exc).__name__.lower()}"
            self._emit("preflight_rejected", reason=reason, attempts=BETA_READ_RETRY_POLICY.attempts)
            raise

    def _read_with_retry(
        self,
        reader: Callable[[], Any],
        *,
        operation: str,
        retry_event: str = "leg_waiting",
        **fields: Any,
    ) -> Any:
        def on_retry(event: Mapping[str, object]) -> None:
            payload = {
                "operation": operation,
                "attempt": event.get("next_attempt"),
                "max_attempts": event.get("max_attempts"),
                "seconds": event.get("delay_seconds"),
                "error": event.get("error"),
                **fields,
            }
            if retry_event == "leg_waiting":
                payload["waiting_for"] = f"{operation}_retry"
            self._emit(retry_event, **payload)

        return retry_read(
            reader,
            operation=operation,
            policy=BETA_READ_RETRY_POLICY,
            sleep=self.sleep,
            retry_sink=on_retry,
        )

    def _observe_position(
        self,
        venue: LiveAdaptiveMakerVenue,
        *,
        round_number: int,
        sequence: int | str,
        symbol: str,
        action: str,
    ) -> float | None:
        try:
            return retry_read(
                venue.position_quantity,
                operation="position_observation",
                policy=POSITION_READ_RETRY_POLICY,
                sleep=self.sleep,
                retry_sink=lambda event: self._emit(
                    "leg_waiting",
                    round=round_number,
                    sequence=sequence,
                    symbol=symbol,
                    action=action,
                    waiting_for="position_observation_retry",
                    attempt=event.get("next_attempt"),
                    max_attempts=event.get("max_attempts"),
                    seconds=event.get("delay_seconds"),
                    error=event.get("error"),
                ),
            )
        except NETWORK_ERRORS as exc:
            self._emit(
                "position_observation_unavailable",
                round=round_number,
                sequence=sequence,
                symbol=symbol,
                action=action,
                error=type(exc).__name__,
                attempts=POSITION_READ_RETRY_POLICY.attempts,
            )
            return None

    def _observe_orders(
        self,
        lane: ExecutionLane,
        *,
        round_number: int,
        sequence: int | str,
        symbol: str,
        action: str,
    ) -> tuple[list[dict[str, Any]], Any] | None:
        try:
            return self._read_with_retry(
                lambda: (
                    lane.gateway.open_orders(symbol, mode="live"),
                    lane.gateway.algo_orders(symbol),
                ),
                operation="order_observation",
                round=round_number,
                sequence=sequence,
                symbol=symbol,
                action=action,
            )
        except NETWORK_ERRORS as exc:
            self._emit(
                "order_observation_unavailable",
                round=round_number,
                sequence=sequence,
                symbol=symbol,
                action=action,
                error=type(exc).__name__,
                attempts=BETA_READ_RETRY_POLICY.attempts,
            )
            return None

    def _create_lanes(self, plan: BetaVolumePlan) -> dict[str, ExecutionLane]:
        external_gateways = self.lane_gateways is not None
        if external_gateways:
            assert self.lane_gateways is not None
            if set(self.lane_gateways) != {"BTC", "ETH"}:
                raise SafetyError("persistent Beta gateways must contain BTC and ETH")
            gateways = [self.lane_gateways["BTC"], self.lane_gateways["ETH"]]
        else:
            if not callable(self.gateway_factory):
                raise SafetyError("independent gateway factory is required for parallel Beta execution")
            gateways = []
            try:
                for _ in range(2):
                    gateways.append(self.gateway_factory())
            except Exception:
                for gateway in gateways:
                    close = getattr(gateway, "close", None)
                    if callable(close):
                        close()
                raise
        if gateways[0] is gateways[1] or self.gateway in gateways:
            if not external_gateways:
                for gateway in gateways:
                    close = getattr(gateway, "close", None)
                    if callable(close):
                        close()
            raise SafetyError("BTC and ETH lanes must use independent gateway instances")
        try:
            return {
                "BTC": ExecutionLane(
                    gateways[0],
                    self._create_venue(gateways[0], "BTC", plan.btc.position_side),
                    self.reconciler_factory(gateways[0]),
                ),
                "ETH": ExecutionLane(
                    gateways[1],
                    self._create_venue(gateways[1], "ETH", plan.eth.position_side),
                    self.reconciler_factory(gateways[1]),
                ),
            }
        except Exception:
            if not external_gateways:
                for gateway in gateways:
                    close = getattr(gateway, "close", None)
                    if callable(close):
                        close()
            raise

    def _create_venue(self, gateway: WeexGateway, symbol: str, position_side: str) -> LiveAdaptiveMakerVenue:
        if self.venue_factory is not None:
            return self.venue_factory(gateway, symbol, position_side)
        return LiveAdaptiveMakerVenue(
            gateway,
            symbol,
            position_side,
            market_data=self.market_data,
            order_updates=self.order_updates,
        )
