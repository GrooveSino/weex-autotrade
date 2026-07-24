from __future__ import annotations

import hashlib
import json
import math
import os
import threading
import time
from collections import defaultdict
from collections.abc import Callable, Mapping
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, replace
from decimal import ROUND_CEILING, ROUND_UP, Decimal, localcontext
from pathlib import Path
from typing import Any

from weex_cli.adaptive_executor import (
    ObservationUnavailableError,
    TargetExecutionResult,
    TargetRequest,
    execute_adaptive_maker_target,
)
from weex_cli.adaptive_maker import AdaptiveMakerPolicy
from weex_cli.adaptive_volume import REAL_POLICY
from weex_cli.beta_allocation import BetaAllocation, HttpBetaAllocationProvider
from weex_cli.errors import SafetyError, ValidationError
from weex_cli.execution_reconciliation import (
    LegFillReconciler,
    LegFillReport,
    LegFillRequest,
    LiveLegFillReconciler,
)
from weex_cli.gateway import WeexGateway, summarize_position_size
from weex_cli.live_maker_venue import LiveAdaptiveMakerVenue
from weex_cli.models import decimal_text, decimal_value
from weex_cli.reliability import NETWORK_ERRORS, ReadRetryPolicy, retry_read

PLAN_MAX_AGE_SECONDS = 900
MAX_BETA_DRIFT = Decimal("0.05")
MAX_PRICE_DRIFT = Decimal("0.01")
MARGIN_BUFFER = Decimal("1.20")
MAX_AUTO_LEVERAGE = 99
MAX_FIXED_LEVERAGE = 400
DEFAULT_STRATEGY_DIRECTION = "btc_long_eth_short"
STRATEGY_DIRECTIONS = {DEFAULT_STRATEGY_DIRECTION, "btc_short_eth_long"}
POST_FLAT_ACCOUNTING_ATTEMPTS = 8
BETA_READ_RETRY_POLICY = ReadRetryPolicy(attempts=8, initial_delay_seconds=1, max_delay_seconds=8)
POSITION_READ_RETRY_POLICY = ReadRetryPolicy(attempts=6, initial_delay_seconds=0.5, max_delay_seconds=4)
RETRYABLE_ACCOUNTING_STATUSES = {"fills_not_visible", "fill_source_incomplete", "quantity_mismatch"}
DEFAULT_PLAN_DIRECTORY = Path("data/beta-volume-plans")
PhaseWaiter = Callable[[str, str, int], bool]


@dataclass(frozen=True)
class PairLegPlan:
    symbol: str
    position_side: str
    opening_side: str
    closing_side: str
    allocated_quote: Decimal
    reference_price: Decimal
    quantity: Decimal
    amount_step: Decimal
    open_client_prefix: str
    close_client_prefix: str

    def as_dict(self) -> dict[str, str]:
        return {
            "symbol": self.symbol,
            "position_side": self.position_side,
            "opening_side": self.opening_side,
            "closing_side": self.closing_side,
            "allocated_quote": decimal_text(self.allocated_quote) or "0",
            "reference_price": decimal_text(self.reference_price) or "0",
            "quantity": decimal_text(self.quantity) or "0",
            "amount_step": decimal_text(self.amount_step) or "0",
            "open_client_order_id": f"{self.open_client_prefix}-001",
            "close_client_order_id": f"{self.close_client_prefix}-001",
            "time_in_force": "POST_ONLY",
        }


@dataclass(frozen=True)
class BetaVolumePlan:
    schema_version: int
    plan_id: str
    created_at_ms: int
    target_turnover_quote: Decimal
    round_turnover_quote: Decimal
    opening_budget_quote: Decimal
    max_position_quote: Decimal
    timeout_seconds: int
    recovery_attempts: int
    max_empty_rounds: int
    cooldown_seconds: float
    leverage: str | int
    max_auto_leverage: int
    margin_buffer: Decimal
    margin_mode: str
    allocation: BetaAllocation
    btc: PairLegPlan
    eth: PairLegPlan
    estimated_turnover_quote: Decimal
    direction: str = DEFAULT_STRATEGY_DIRECTION

    @classmethod
    def create(
        cls,
        gateway: WeexGateway,
        allocation: BetaAllocation,
        *,
        target_turnover_quote: str | Decimal,
        round_turnover_quote: str | Decimal = "500",
        max_position_quote: str | Decimal,
        timeout_seconds: int,
        recovery_attempts: int = 3,
        max_empty_rounds: int = 3,
        cooldown_seconds: float = 1.0,
        leverage: str | int = "auto",
        max_auto_leverage: int = MAX_AUTO_LEVERAGE,
        margin_buffer: str | Decimal = MARGIN_BUFFER,
        margin_mode: str = "isolated",
        direction: str = DEFAULT_STRATEGY_DIRECTION,
        now_ms: int | None = None,
    ) -> BetaVolumePlan:
        target = decimal_value(target_turnover_quote, name="target_turnover_quote")
        per_round = decimal_value(round_turnover_quote, name="round_turnover_quote")
        max_position = decimal_value(max_position_quote, name="max_position_quote")
        assert target is not None and per_round is not None and max_position is not None
        normalized_leverage = _normalize_leverage(leverage)
        normalized_margin_mode = _normalize_margin_mode(margin_mode)
        normalized_direction = _normalize_direction(direction)
        normalized_margin_buffer = decimal_value(margin_buffer, name="margin_buffer")
        assert normalized_margin_buffer is not None
        if timeout_seconds < 1:
            raise ValidationError("timeout_seconds must be positive")
        if not 1 <= max_auto_leverage <= 125:
            raise ValidationError("max_auto_leverage must be between 1 and 125")
        if normalized_margin_buffer < 1:
            raise ValidationError("margin_buffer must be at least 1")
        if not 1 <= recovery_attempts <= 10:
            raise ValidationError("recovery_attempts must be between 1 and 10")
        if not 0 <= max_empty_rounds <= 20:
            raise ValidationError("max_empty_rounds must be between 0 and 20")
        if not math.isfinite(cooldown_seconds) or not 0 <= cooldown_seconds <= 300:
            raise ValidationError("cooldown_seconds must be finite and between 0 and 300")
        per_round = min(per_round, target)
        opening_budget = per_round / 2
        btc_quote = opening_budget * allocation.btc_long_weight
        eth_quote = opening_budget * allocation.eth_short_weight
        btc_price = _mid_price(gateway, "BTC")
        eth_price = _mid_price(gateway, "ETH")
        btc_step = gateway.amount_step("BTC")
        eth_step = gateway.amount_step("ETH")

        minimum_target = max(
            Decimal(2) * btc_step * btc_price / allocation.btc_long_weight,
            Decimal(2) * eth_step * eth_price / allocation.eth_short_weight,
        )
        if btc_quote < btc_step * btc_price or eth_quote < eth_step * eth_price:
            suggested = minimum_target.quantize(Decimal("0.01"), rounding=ROUND_UP)
            raise ValidationError(
                "target turnover is below the current BTC/ETH minimum-size allocation; "
                f"current minimum is approximately {suggested} USDT"
            )

        btc_quantity, eth_quantity, estimated_turnover = _choose_pair_quantities(
            gateway,
            per_round,
            btc_quote,
            eth_quote,
            btc_price,
            eth_price,
            btc_step,
            eth_step,
        )
        if btc_quantity * btc_price >= max_position or eth_quantity * eth_price >= max_position:
            raise ValidationError("a planned leg reaches or exceeds max_position_quote")

        created_at_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        identity = "|".join(
            (
                allocation.version,
                decimal_text(target) or "0",
                decimal_text(per_round) or "0",
                decimal_text(btc_quantity) or "0",
                decimal_text(eth_quantity) or "0",
                decimal_text(max_position) or "0",
                str(timeout_seconds),
                str(recovery_attempts),
                str(max_empty_rounds),
                str(cooldown_seconds),
                str(normalized_leverage),
                str(max_auto_leverage),
                decimal_text(normalized_margin_buffer) or "0",
                normalized_margin_mode,
                normalized_direction,
                str(created_at_ms),
            )
        )
        digest = hashlib.sha256(identity.encode("ascii")).hexdigest()[:10]
        plan_id = f"wv-{digest}"
        btc_position, btc_open, btc_close = _direction_sides(normalized_direction, "BTC")
        eth_position, eth_open, eth_close = _direction_sides(normalized_direction, "ETH")
        btc = PairLegPlan(
            symbol="BTC",
            position_side=btc_position,
            opening_side=btc_open,
            closing_side=btc_close,
            allocated_quote=btc_quote,
            reference_price=btc_price,
            quantity=btc_quantity,
            amount_step=btc_step,
            open_client_prefix=f"{plan_id}-bo",
            close_client_prefix=f"{plan_id}-bc",
        )
        eth = PairLegPlan(
            symbol="ETH",
            position_side=eth_position,
            opening_side=eth_open,
            closing_side=eth_close,
            allocated_quote=eth_quote,
            reference_price=eth_price,
            quantity=eth_quantity,
            amount_step=eth_step,
            open_client_prefix=f"{plan_id}-eo",
            close_client_prefix=f"{plan_id}-ec",
        )
        return cls(
            schema_version=4,
            plan_id=plan_id,
            created_at_ms=created_at_ms,
            target_turnover_quote=target,
            round_turnover_quote=per_round,
            opening_budget_quote=opening_budget,
            max_position_quote=max_position,
            timeout_seconds=timeout_seconds,
            recovery_attempts=recovery_attempts,
            max_empty_rounds=max_empty_rounds,
            cooldown_seconds=cooldown_seconds,
            leverage=normalized_leverage,
            max_auto_leverage=max_auto_leverage,
            margin_buffer=normalized_margin_buffer,
            margin_mode=normalized_margin_mode,
            direction=normalized_direction,
            allocation=allocation,
            btc=btc,
            eth=eth,
            estimated_turnover_quote=estimated_turnover,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "schema_version": self.schema_version,
            "created_at_ms": self.created_at_ms,
            "expires_at_ms": self.created_at_ms + PLAN_MAX_AGE_SECONDS * 1000,
            "mode": "live",
            "strategy": self.direction,
            "target_turnover_quote": decimal_text(self.target_turnover_quote),
            "round_turnover_quote": decimal_text(self.round_turnover_quote),
            "opening_budget_quote": decimal_text(self.opening_budget_quote),
            "max_position_quote": decimal_text(self.max_position_quote),
            "timeout_seconds": self.timeout_seconds,
            "recovery_attempts": self.recovery_attempts,
            "max_empty_rounds": self.max_empty_rounds,
            "cooldown_seconds": self.cooldown_seconds,
            "leverage": self.leverage,
            "max_auto_leverage": self.max_auto_leverage,
            "margin_buffer": decimal_text(self.margin_buffer),
            "margin_mode": self.margin_mode,
            "direction": self.direction,
            "minimum_available_quote": decimal_text(self.required_available_quote),
            "allocation": self.allocation.as_dict(),
            "legs": [self.btc.as_dict(), self.eth.as_dict()],
            "estimated_turnover_quote": decimal_text(self.estimated_turnover_quote),
            "estimated_rounds": self.estimated_rounds,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> BetaVolumePlan:
        allocation_row = payload["allocation"]
        legs = payload["legs"]
        if not isinstance(allocation_row, Mapping) or not isinstance(legs, list) or len(legs) != 2:
            raise ValidationError("stored Beta plan is invalid")
        allocation = BetaAllocation(
            beta=Decimal(str(allocation_row["beta"])),
            btc_long_weight=Decimal(str(allocation_row["btc_long_weight"])),
            eth_short_weight=Decimal(str(allocation_row["eth_short_weight"])),
            version=str(allocation_row["version"]),
            as_of_ms=int(allocation_row["as_of_ms"]),
            confidence=Decimal(str(allocation_row["confidence"])),
            confidence_threshold=Decimal(str(allocation_row["confidence_threshold"])),
            source=str(allocation_row["source"]),
            confidence_override=bool(allocation_row.get("confidence_override", False)),
        )
        return cls(
            schema_version=int(payload.get("schema_version", 1)),
            plan_id=str(payload["plan_id"]),
            created_at_ms=int(payload["created_at_ms"]),
            target_turnover_quote=Decimal(str(payload["target_turnover_quote"])),
            round_turnover_quote=Decimal(str(payload.get("round_turnover_quote", payload["target_turnover_quote"]))),
            opening_budget_quote=Decimal(str(payload["opening_budget_quote"])),
            max_position_quote=Decimal(str(payload["max_position_quote"])),
            timeout_seconds=int(payload["timeout_seconds"]),
            recovery_attempts=int(payload.get("recovery_attempts", 3)),
            max_empty_rounds=int(payload.get("max_empty_rounds", 3)),
            cooldown_seconds=float(payload.get("cooldown_seconds", 1.0)),
            leverage=_normalize_leverage(payload.get("leverage", 1)),
            max_auto_leverage=int(payload.get("max_auto_leverage", MAX_AUTO_LEVERAGE)),
            margin_buffer=Decimal(str(payload.get("margin_buffer", MARGIN_BUFFER))),
            margin_mode=_normalize_margin_mode(payload.get("margin_mode", "isolated")),
            direction=_normalize_direction(
                payload.get("direction", payload.get("strategy", DEFAULT_STRATEGY_DIRECTION))
            ),
            allocation=allocation,
            btc=_leg_from_dict(legs[0]),
            eth=_leg_from_dict(legs[1]),
            estimated_turnover_quote=Decimal(str(payload["estimated_turnover_quote"])),
        )

    @property
    def required_available_quote(self) -> Decimal:
        opening_notional = self.estimated_turnover_quote / 2
        leverage = self.max_auto_leverage if self.leverage == "auto" else int(self.leverage)
        return opening_notional / Decimal(leverage) * self.margin_buffer

    @property
    def estimated_rounds(self) -> int:
        return int((self.target_turnover_quote / self.round_turnover_quote).to_integral_value(rounding=ROUND_UP))


class BetaVolumePlanStore:
    def __init__(self, directory: Path = DEFAULT_PLAN_DIRECTORY) -> None:
        self.directory = directory

    def save(self, plan: BetaVolumePlan, *, state: str = "planned", result: Any = None) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f"{plan.plan_id}.json"
        temporary = path.with_suffix(".tmp")
        payload = {"schema_version": plan.schema_version, "state": state, "plan": plan.as_dict(), "result": result}
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temporary, path)
        return path

    def create(self, plan: BetaVolumePlan) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f"{plan.plan_id}.json"
        payload = {"schema_version": plan.schema_version, "state": "planned", "plan": plan.as_dict(), "result": None}
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        try:
            descriptor = os.open(path, flags, 0o600)
        except FileExistsError:
            raise SafetyError(f"Beta plan already exists: {plan.plan_id}") from None
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return path

    def claim_for_execution(self, plan: BetaVolumePlan) -> None:
        record = self.load_record(plan.plan_id)
        if record.plan != plan or record.state != "planned":
            raise SafetyError(
                f"plan {plan.plan_id} is not in a pristine planned state; inspect live state before recovery"
            )
        claim_path = self.directory / f"{plan.plan_id}.claim"
        try:
            descriptor = os.open(claim_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            raise SafetyError(f"plan {plan.plan_id} is already claimed or consumed") from None
        try:
            with os.fdopen(descriptor, "w", encoding="ascii") as handle:
                handle.write(str(int(time.time() * 1000)))
                handle.flush()
                os.fsync(handle.fileno())
            current = self.load_record(plan.plan_id)
            if current.plan != plan or current.state != "planned":
                claim_path.unlink(missing_ok=True)
                raise SafetyError(f"plan {plan.plan_id} changed before it could be claimed")
            self.save(plan, state="executing")
        except Exception:
            # A failed state transition remains claimed unless it was proven not to have started.
            raise

    def claim_for_recovery(self, plan: BetaVolumePlan, symbol: str | None = None) -> None:
        record = self.load_record(plan.plan_id)
        if record.state not in {"uncertain", "stopped", "recovery_uncertain"}:
            raise SafetyError(f"plan {plan.plan_id} is not in a recoverable state")
        suffix = f".{symbol.strip().lower()}" if symbol else ""
        claim_path = self.directory / f"{plan.plan_id}{suffix}.recovery.claim"
        try:
            descriptor = os.open(claim_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            raise SafetyError(f"recovery for plan {plan.plan_id} is already claimed") from None
        with os.fdopen(descriptor, "w", encoding="ascii") as handle:
            handle.write(str(int(time.time() * 1000)))
            handle.flush()
            os.fsync(handle.fileno())

    def save_recovery(self, plan: BetaVolumePlan, result: Any, symbol: str | None = None) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        suffix = f".{symbol.strip().lower()}" if symbol else ""
        path = self.directory / f"{plan.plan_id}{suffix}.recovery.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temporary, path)
        return path

    def load(self, plan_id: str) -> tuple[BetaVolumePlan, str]:
        record = self.load_record(plan_id)
        return record.plan, record.state

    def load_record(self, plan_id: str) -> BetaVolumePlanRecord:
        plan_id = plan_id.lower()
        if not plan_id.startswith("wv-") or not plan_id[3:].isalnum():
            raise ValidationError("invalid Beta plan ID")
        path = self.directory / f"{plan_id}.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise ValidationError(f"Beta plan not found: {plan_id}") from None
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            raise ValidationError(f"Beta plan is unreadable: {plan_id}") from None
        if not isinstance(payload, Mapping) or payload.get("schema_version") not in {1, 2, 3, 4}:
            raise ValidationError("stored Beta plan schema is invalid")
        plan_row = payload.get("plan")
        if not isinstance(plan_row, Mapping):
            raise ValidationError("stored Beta plan payload is invalid")
        return BetaVolumePlanRecord(
            plan=BetaVolumePlan.from_dict(plan_row),
            state=str(payload.get("state") or "unknown"),
            result=payload.get("result"),
        )


@dataclass(frozen=True)
class BetaVolumePlanRecord:
    plan: BetaVolumePlan
    state: str
    result: Any = None


VenueFactory = Callable[[WeexGateway, str, str], LiveAdaptiveMakerVenue]
GatewayFactory = Callable[[], WeexGateway]
ReconcilerFactory = Callable[[WeexGateway], LegFillReconciler]
EventSink = Callable[[Mapping[str, Any]], None]
DelaySelector = Callable[[int], float]


@dataclass(frozen=True)
class _LegSpec:
    plan: PairLegPlan
    action: str
    side: str
    target_position: float
    client_prefix: str


@dataclass(frozen=True)
class _Lane:
    gateway: WeexGateway
    venue: LiveAdaptiveMakerVenue
    reconciler: LegFillReconciler


@dataclass(frozen=True)
class _PendingFillReconciliation:
    request: LegFillRequest
    executor_status: str
    executor_reason: str


class LiveBetaVolumeService:
    PAIR_HEARTBEAT_SECONDS = 5.0

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
        beta_drift = abs(current.beta - plan.allocation.beta) / plan.allocation.beta
        if beta_drift > MAX_BETA_DRIFT:
            raise SafetyError("Beta moved more than 5% since planning; create and review a new dry run")
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
        return {**account, "beta_drift": decimal_text(beta_drift), "fresh_beta_version": current.version}

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
        lane: _Lane,
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

    def recover(self, plan: BetaVolumePlan, symbol: str, quantity: Decimal) -> dict[str, Any]:
        normalized_symbol = symbol.upper()
        if normalized_symbol not in {"BTC", "ETH"}:
            raise ValidationError("recovery symbol must be BTC or ETH")
        leg_plan = plan.btc if normalized_symbol == "BTC" else plan.eth
        position_side = leg_plan.position_side
        current = observed_recovery_quantity(self.gateway, normalized_symbol, position_side)
        if current <= leg_plan.amount_step / 2:
            return {
                "schema_version": 1,
                "kind": "beta_volume_recovery",
                "mode": "live",
                "status": "completed",
                "reason": "already_flat",
                "plan_id": plan.plan_id,
                "symbol": normalized_symbol,
                "position_side": position_side,
                "maker_only": True,
                "executed_quote_volume": "0",
                "final_position": "0",
                "reconciliation_required": False,
            }
        if abs(current - quantity) > leg_plan.amount_step / 2:
            raise SafetyError("recovery quantity changed since dry run; create a new recovery dry run")
        if self.gateway.open_orders(normalized_symbol, mode="live") or _row_count(
            self.gateway.algo_orders(normalized_symbol)
        ):
            raise SafetyError("recovery requires no active regular or trigger orders")
        venue = self._create_venue(self.gateway, normalized_symbol, position_side)
        lane = _Lane(self.gateway, venue, self.reconciler_factory(self.gateway))
        close_plan = replace(leg_plan, quantity=quantity, allocated_quote=Decimal(0))
        summaries, _, stop = self._flatten_lane(plan, 1, 1, close_plan, lane)
        final_position = self._observe_position(
            venue,
            round_number=1,
            sequence="recovery",
            symbol=normalized_symbol,
            action="close",
        )
        accounting = _accounting_summary(summaries)
        flat = final_position is not None and abs(Decimal(str(final_position))) <= leg_plan.amount_step / 2
        no_orders = (
            not self.gateway.open_orders(normalized_symbol, mode="live")
            and _row_count(self.gateway.algo_orders(normalized_symbol)) == 0
        )
        completed = flat and no_orders and accounting["verified"] and accounting["maker_only"] and stop is None
        status = "completed" if completed else "uncertain" if stop and _is_uncertain_stop(stop) else "stopped"
        result = {
            "schema_version": 1,
            "kind": "beta_volume_recovery",
            "mode": "live",
            "status": status,
            "reason": "maker_recovery_completed" if completed else (stop[1] if stop else "recovery_invariant_failed"),
            "plan_id": plan.plan_id,
            "symbol": normalized_symbol,
            "position_side": position_side,
            "maker_only": accounting["maker_only"],
            "executed_quote_volume": accounting["executed_quote_volume"],
            "accounting": accounting,
            "legs": summaries,
            "final_position": final_position,
            "reconciliation_required": not completed,
            "retry_allowed": False,
        }
        self.store.save_recovery(plan, result, normalized_symbol)
        return result

    def cleanup(self, plan: BetaVolumePlan) -> dict[str, Any]:
        """Run the existing single-pass safe-stop convergence for a persisted plan."""
        lanes = self._create_lanes(plan)
        return self._safe_stop(
            plan,
            lanes,
            {},
            self.now_ms(),
            summaries=[],
            cycles=[],
            total_quote=Decimal(0),
            round_number=1,
        )

    def _create_lanes(self, plan: BetaVolumePlan) -> dict[str, _Lane]:
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
                "BTC": _Lane(
                    gateways[0],
                    self._create_venue(gateways[0], "BTC", plan.btc.position_side),
                    self.reconciler_factory(gateways[0]),
                ),
                "ETH": _Lane(
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

    def _execute_cycles(
        self,
        plan: BetaVolumePlan,
        lanes: Mapping[str, _Lane],
        preflight: Mapping[str, Any],
        execution_started_ms: int,
    ) -> dict[str, Any]:
        summaries: list[dict[str, Any]] = []
        cycles: list[dict[str, Any]] = []
        total_quote = Decimal(0)
        empty_rounds = 0
        max_rounds = plan.estimated_rounds * 3 + plan.max_empty_rounds + 5

        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="weex-beta") as pool:
            for round_number in range(1, max_rounds + 1):
                if self.stop_requested():
                    return self._safe_stop(
                        plan,
                        lanes,
                        preflight,
                        execution_started_ms,
                        summaries=summaries,
                        cycles=cycles,
                        total_quote=total_quote,
                        round_number=round_number,
                        pool=pool,
                    )
                if total_quote >= plan.target_turnover_quote:
                    break
                if self.phase_waiter is not None and not self.phase_waiter(plan.plan_id, "open", round_number):
                    return self._safe_stop(
                        plan,
                        lanes,
                        preflight,
                        execution_started_ms,
                        summaries=summaries,
                        cycles=cycles,
                        total_quote=total_quote,
                        round_number=round_number,
                        pool=pool,
                    )
                if self.phase_waiter is not None:
                    preflight = self._preflight_with_read_retry(plan)
                desired_quote = min(plan.round_turnover_quote, plan.target_turnover_quote - total_quote)
                self._emit(
                    "cycle_preparing",
                    round=round_number,
                    desired_quote=decimal_text(desired_quote),
                )
                try:
                    btc_plan, eth_plan, sizing = self._read_with_retry(
                        lambda desired_quote=desired_quote: _size_cycle(plan, lanes, desired_quote),
                        operation="cycle_sizing",
                        retry_event="cycle_sizing_retry",
                        round=round_number,
                    )
                except Exception as exc:  # noqa: BLE001 - sizing happens only at a proven flat boundary
                    return self._finish(
                        plan,
                        "stopped",
                        f"cycle_sizing:{type(exc).__name__.lower()}",
                        summaries,
                        cycles,
                        total_quote,
                        lanes,
                        preflight,
                        execution_started_ms,
                    )
                self._emit(
                    "leverage_preparing",
                    round=round_number,
                    opening_notional_quote=sizing["opening_notional_quote"],
                )
                try:
                    selected_leverage, leverage_state = self._prepare_cycle_leverage(
                        plan,
                        Decimal(sizing["opening_notional_quote"]),
                        round_number,
                    )
                except Exception as exc:  # noqa: BLE001 - no order is submitted until leverage is proven
                    reason = _cycle_leverage_failure_reason(exc)
                    self._emit("cycle_stopped", round=round_number, status="stopped", reason=reason)
                    return self._finish(
                        plan,
                        "stopped",
                        reason,
                        summaries,
                        cycles,
                        total_quote,
                        lanes,
                        preflight,
                        execution_started_ms,
                    )
                self._emit(
                    "cycle_started",
                    round=round_number,
                    desired_quote=decimal_text(desired_quote),
                    btc_quantity=decimal_text(btc_plan.quantity),
                    eth_quantity=decimal_text(eth_plan.quantity),
                    leverage=selected_leverage,
                )
                cycle_started_ms = self.now_ms()
                open_specs = {
                    "BTC": _LegSpec(
                        btc_plan,
                        "open",
                        btc_plan.opening_side,
                        _signed_open_quantity(btc_plan),
                        f"{plan.plan_id}-r{round_number:03d}-bo",
                    ),
                    "ETH": _LegSpec(
                        eth_plan,
                        "open",
                        eth_plan.opening_side,
                        _signed_open_quantity(eth_plan),
                        f"{plan.plan_id}-r{round_number:03d}-eo",
                    ),
                }
                open_results = self._run_pair(pool, plan, round_number, 1, open_specs, lanes)
                open_summaries = [open_results[symbol][0] for symbol in ("BTC", "ETH")]
                summaries.extend(open_summaries)

                if self.stop_requested():
                    return self._safe_stop(
                        plan,
                        lanes,
                        preflight,
                        execution_started_ms,
                        summaries=summaries,
                        cycles=cycles,
                        total_quote=total_quote,
                        round_number=round_number,
                        pool=pool,
                    )

                lane_stops: dict[str, tuple[str, str]] = {
                    symbol: result[1] for symbol, result in open_results.items() if result[1] is not None
                }
                hold_seconds = self._hold_open_pair(
                    round_number,
                    lane_stops,
                    lanes,
                    btc_plan,
                    eth_plan,
                )
                if self.stop_requested():
                    return self._safe_stop(
                        plan,
                        lanes,
                        preflight,
                        execution_started_ms,
                        summaries=summaries,
                        cycles=cycles,
                        total_quote=total_quote,
                        round_number=round_number,
                        pool=pool,
                    )
                if self.phase_waiter is not None and not self.phase_waiter(plan.plan_id, "close", round_number):
                    return self._safe_stop(
                        plan,
                        lanes,
                        preflight,
                        execution_started_ms,
                        summaries=summaries,
                        cycles=cycles,
                        total_quote=total_quote,
                        round_number=round_number,
                        pool=pool,
                    )
                if self.phase_waiter is not None and not self._close_phase_boundary_ready(
                    plan, lanes, round_number
                ):
                    return self._safe_stop(
                        plan,
                        lanes,
                        preflight,
                        execution_started_ms,
                        summaries=summaries,
                        cycles=cycles,
                        total_quote=total_quote,
                        round_number=round_number,
                        pool=pool,
                    )
                close_futures: dict[str, Future[tuple[list[dict[str, Any]], bool, tuple[str, str] | None]]] = {}
                self._emit("close_barrier_started", round=round_number)
                for offset, symbol in enumerate(("BTC", "ETH"), 3):
                    stop = lane_stops.get(symbol)
                    if stop is not None and stop[0] == "submission_uncertain":
                        continue
                    position = self._observe_position(
                        lanes[symbol].venue,
                        round_number=round_number,
                        sequence="barrier",
                        symbol=symbol,
                        action="close",
                    )
                    if position is None:
                        lane_stops[symbol] = ("observation_uncertain", "position_observation_unavailable")
                        continue
                    leg_plan = btc_plan if symbol == "BTC" else eth_plan
                    if abs(Decimal(str(position))) <= leg_plan.amount_step / 2:
                        continue
                    close_futures[symbol] = pool.submit(
                        self._flatten_lane,
                        plan,
                        round_number,
                        offset,
                        leg_plan,
                        lanes[symbol],
                    )

                close_summaries: list[dict[str, Any]] = []
                if close_futures:
                    self._emit(
                        "pair_waiting",
                        round=round_number,
                        action="close",
                        symbols=tuple(close_futures),
                    )
                for symbol in ("BTC", "ETH"):
                    future = close_futures.get(symbol)
                    if future is None:
                        continue
                    lane_summaries, _, close_stop = future.result()
                    close_summaries.extend(lane_summaries)
                    if close_stop is not None:
                        lane_stops[symbol] = close_stop
                self._emit("pair_wait_completed", round=round_number, action="close")
                summaries.extend(close_summaries)

                if self.stop_requested():
                    return self._safe_stop(
                        plan,
                        lanes,
                        preflight,
                        execution_started_ms,
                        summaries=summaries,
                        cycles=cycles,
                        total_quote=total_quote,
                        round_number=round_number,
                        pool=pool,
                    )

                cycle_legs = open_summaries + close_summaries
                self._refresh_pending_accounting(round_number, cycle_legs, lanes, lane_stops)
                positions = {
                    symbol: self._observe_position(
                        lane.venue,
                        round_number=round_number,
                        sequence="checkpoint",
                        symbol=symbol,
                        action="cycle_check",
                    )
                    for symbol, lane in lanes.items()
                }
                flat = all(
                    positions[symbol] is not None
                    and abs(Decimal(str(positions[symbol])))
                    <= (btc_plan.amount_step if symbol == "BTC" else eth_plan.amount_step) / 2
                    for symbol in ("BTC", "ETH")
                )
                cycle_quote = sum((Decimal(str(row.get("quote_volume") or 0)) for row in cycle_legs), Decimal(0))
                total_quote += cycle_quote
                open_btc_quote = Decimal(str(open_summaries[0].get("quote_volume") or 0))
                open_eth_quote = Decimal(str(open_summaries[1].get("quote_volume") or 0))
                actual_beta = open_eth_quote / open_btc_quote if open_btc_quote > 0 else None
                cycle_accounting = _accounting_summary(cycle_legs)
                uncertain = any(_is_uncertain_stop(stop) for stop in lane_stops.values())
                hard_reason = _terminal_reason(lane_stops)
                if uncertain:
                    cycle_status = "uncertain"
                elif not flat:
                    cycle_status = "stopped"
                    hard_reason = hard_reason or "paired_cycle_not_flat"
                elif hard_reason is not None:
                    cycle_status = "stopped"
                elif cycle_quote == 0:
                    cycle_status = "empty"
                elif lane_stops:
                    cycle_status = "recovered"
                else:
                    cycle_status = "completed"
                projected_empty_rounds = empty_rounds + 1 if cycle_quote == 0 else 0
                safe_to_continue = (
                    not uncertain
                    and hard_reason is None
                    and flat
                    and projected_empty_rounds <= plan.max_empty_rounds
                    and total_quote < plan.target_turnover_quote
                )
                round_gap_seconds = (
                    self._delay_seconds(self.round_gap_delay_seconds, round_number, plan.cooldown_seconds)
                    if safe_to_continue
                    else 0.0
                )
                cycle = {
                    "round": round_number,
                    "status": cycle_status,
                    "reason": hard_reason or ("paired_cycle_flat" if flat else "paired_cycle_not_flat"),
                    "desired_quote": decimal_text(desired_quote),
                    "executed_quote_volume": decimal_text(cycle_quote),
                    "cumulative_quote_volume": decimal_text(total_quote),
                    "planned_open_beta": sizing["planned_open_beta"],
                    "actual_open_beta": decimal_text(actual_beta),
                    "leverage": selected_leverage,
                    "leverage_state": leverage_state,
                    "hold_seconds": hold_seconds,
                    "round_gap_seconds": round_gap_seconds,
                    "flat": flat,
                    "positions": positions,
                    "accounting": cycle_accounting,
                    "elapsed_ms": self.now_ms() - cycle_started_ms,
                    "legs": cycle_legs,
                }
                cycles.append(cycle)
                self._emit(
                    "cycle_completed" if cycle_status in {"completed", "recovered"} else "cycle_stopped",
                    round=round_number,
                    status=cycle_status,
                    reason=cycle["reason"],
                    quote_volume=decimal_text(cycle_quote),
                    total_quote=decimal_text(total_quote),
                    elapsed_ms=cycle["elapsed_ms"],
                )
                checkpoint = _result_payload(
                    plan,
                    "executing",
                    "cycle_checkpointed",
                    summaries,
                    cycles,
                    total_quote,
                    {symbol: lane.venue for symbol, lane in lanes.items()},
                    preflight,
                    self.timeline,
                    self.now_ms() - execution_started_ms,
                )
                self.store.save(plan, state="executing", result=checkpoint)

                if uncertain:
                    return self._finish(
                        plan,
                        "uncertain",
                        hard_reason or "lane_execution_uncertain",
                        summaries,
                        cycles,
                        total_quote,
                        lanes,
                        preflight,
                        execution_started_ms,
                    )
                if hard_reason is not None or not flat:
                    return self._finish(
                        plan,
                        "stopped",
                        hard_reason or "paired_cycle_not_flat",
                        summaries,
                        cycles,
                        total_quote,
                        lanes,
                        preflight,
                        execution_started_ms,
                    )
                if cycle_quote == 0:
                    empty_rounds += 1
                    if empty_rounds > plan.max_empty_rounds:
                        return self._finish(
                            plan,
                            "stopped",
                            "empty_round_limit_exhausted",
                            summaries,
                            cycles,
                            total_quote,
                            lanes,
                            preflight,
                            execution_started_ms,
                        )
                else:
                    empty_rounds = 0
                if total_quote < plan.target_turnover_quote and round_gap_seconds:
                    self._emit("round_gap_started", round=round_number, seconds=round_gap_seconds)
                    self._wait_for_stop(round_gap_seconds)
                    if self.stop_requested():
                        return self._safe_stop(
                            plan,
                            lanes,
                            preflight,
                            execution_started_ms,
                            summaries=summaries,
                            cycles=cycles,
                            total_quote=total_quote,
                            round_number=round_number,
                            pool=pool,
                        )
                    self._emit("round_gap_completed", round=round_number, seconds=round_gap_seconds)

        if total_quote < plan.target_turnover_quote:
            return self._finish(
                plan,
                "stopped",
                "round_limit_exhausted",
                summaries,
                cycles,
                total_quote,
                lanes,
                preflight,
                execution_started_ms,
            )
        return self._final_acceptance(plan, summaries, cycles, total_quote, lanes, preflight, execution_started_ms)

    def _hold_open_pair(
        self,
        round_number: int,
        lane_stops: dict[str, tuple[str, str]],
        lanes: Mapping[str, _Lane],
        btc_plan: PairLegPlan,
        eth_plan: PairLegPlan,
    ) -> float:
        if lane_stops:
            return 0.0
        positions = {
            symbol: self._observe_position(
                lane.venue,
                round_number=round_number,
                sequence="hold",
                symbol=symbol,
                action="hold_check",
            )
            for symbol, lane in lanes.items()
        }
        if any(positions[symbol] is None for symbol in ("BTC", "ETH")):
            for symbol in ("BTC", "ETH"):
                if positions[symbol] is None:
                    lane_stops[symbol] = ("observation_uncertain", "position_observation_unavailable")
            return 0.0
        expected_positions = {
            "BTC": Decimal(str(_signed_open_quantity(btc_plan))),
            "ETH": Decimal(str(_signed_open_quantity(eth_plan))),
        }
        tolerances = {
            "BTC": btc_plan.amount_step / 2,
            "ETH": eth_plan.amount_step / 2,
        }
        targets_reached = all(
            abs(Decimal(str(positions[symbol])) - expected_positions[symbol]) <= tolerances[symbol]
            for symbol in ("BTC", "ETH")
        )
        if not targets_reached:
            self._emit("open_barrier_not_ready", round=round_number)
            return 0.0
        seconds = self._delay_seconds(self.hold_delay_seconds, round_number, 0.0)
        if seconds:
            self._emit("open_barrier_verified", round=round_number)
            self._emit("hold_started", round=round_number, seconds=seconds)
            self._wait_for_stop(seconds)
            if self.stop_requested():
                return seconds
            self._emit("hold_completed", round=round_number, seconds=seconds)
        return seconds

    def _close_phase_boundary_ready(
        self,
        plan: BetaVolumePlan,
        lanes: Mapping[str, _Lane],
        round_number: int,
    ) -> bool:
        try:
            if self.provider is None:
                return False
            self._read_with_retry(
                self.provider.get,
                operation="close_beta_observation",
                round=round_number,
            )
            for symbol, lane in lanes.items():
                self._read_with_retry(
                    lambda lane=lane, symbol=symbol: _mid_price(lane.gateway, symbol),
                    operation="close_market_observation",
                    round=round_number,
                    symbol=symbol,
                )
                position = self._observe_position(
                    lane.venue,
                    round_number=round_number,
                    sequence="pacing-boundary",
                    symbol=symbol,
                    action="close",
                )
                orders = self._observe_orders(
                    lane,
                    round_number=round_number,
                    sequence="pacing-boundary",
                    symbol=symbol,
                    action="close",
                )
                if position is None or orders is None:
                    return False
                active_orders, trigger_orders = orders
                if active_orders or _row_count(trigger_orders):
                    return False
        except Exception:  # noqa: BLE001 - normal close falls through to unpaced safe-stop
            return False
        return True

    def _wait_for_stop(self, seconds: float) -> None:
        """Wait without making stop requests wait for a full hold/gap interval."""
        if seconds <= 0:
            return
        if not self._stop_callback_configured:
            self.sleep(seconds)
            return
        remaining = seconds
        while remaining > 0:
            if self.stop_requested():
                return
            delay = min(0.125, remaining)
            self.sleep(delay)
            remaining -= delay

    def _safe_stop(
        self,
        plan: BetaVolumePlan,
        lanes: Mapping[str, _Lane],
        preflight: Mapping[str, Any],
        execution_started_ms: int,
        *,
        summaries: list[dict[str, Any]],
        cycles: list[dict[str, Any]],
        total_quote: Decimal,
        round_number: int,
        pool: ThreadPoolExecutor | None = None,
    ) -> dict[str, Any]:
        """Cancel all live orders, maker-flatten residuals, then prove the boundary.

        This is deliberately a single convergence path.  It never calls a market
        close endpoint and it never retries cancellation or submission mutations.
        The venue's batch cancellation routine sends one regular and one trigger
        cancellation request, then performs bounded read-only verification.
        """
        self._emit("safe_stop_started", round=round_number)
        cancellation_verified = True
        for symbol in ("BTC", "ETH"):
            cleanup = getattr(lanes[symbol].venue, "cancel_all_and_verify", None)
            if not callable(cleanup):
                cancellation_verified = False
                self._emit(
                    "safe_stop_cancel_unverified",
                    round=round_number,
                    symbol=symbol,
                    reason="cleanup_unavailable",
                )
                continue
            try:
                verified = bool(cleanup())
            except Exception as exc:  # noqa: BLE001 - a cancellation may have landed; fail closed
                verified = False
                self._emit(
                    "safe_stop_cancel_unverified",
                    round=round_number,
                    symbol=symbol,
                    reason=f"cleanup_exception:{type(exc).__name__.lower()}",
                )
            else:
                self._emit(
                    "safe_stop_cancel_verified" if verified else "safe_stop_cancel_unverified",
                    round=round_number,
                    symbol=symbol,
                )
            cancellation_verified = cancellation_verified and verified
        if not cancellation_verified:
            self._emit("safe_stop_uncertain", round=round_number, reason="safe_stop_order_cancellation_unverified")
            return self._finish(
                plan,
                "uncertain",
                "safe_stop_order_cancellation_unverified",
                summaries,
                cycles,
                total_quote,
                lanes,
                preflight,
                execution_started_ms,
            )

        jobs: dict[str, Future[tuple[list[dict[str, Any]], bool, tuple[str, str] | None]]] = {}
        owns_pool = pool is None
        active_pool = pool or ThreadPoolExecutor(max_workers=2, thread_name_prefix="weex-safe-stop")
        try:
            for offset, symbol in enumerate(("BTC", "ETH"), 1):
                leg_plan = plan.btc if symbol == "BTC" else plan.eth
                position = self._observe_position(
                    lanes[symbol].venue,
                    round_number=round_number,
                    sequence="safe-stop",
                    symbol=symbol,
                    action="safe_stop_check",
                )
                if position is None:
                    self._emit(
                        "safe_stop_uncertain",
                        round=round_number,
                        symbol=symbol,
                        reason="position_observation_unavailable",
                    )
                    return self._finish(
                        plan,
                        "uncertain",
                        "safe_stop_position_observation_unavailable",
                        summaries,
                        cycles,
                        total_quote,
                        lanes,
                        preflight,
                        execution_started_ms,
                    )
                if abs(Decimal(str(position))) <= leg_plan.amount_step / 2:
                    continue
                self._emit(
                    "safe_stop_flattening",
                    round=round_number,
                    symbol=symbol,
                    quantity=decimal_text(abs(Decimal(str(position)))),
                )
                jobs[symbol] = active_pool.submit(
                    self._flatten_lane,
                    plan,
                    round_number,
                    100 + offset,
                    leg_plan,
                    lanes[symbol],
                    respect_stop=False,
                )
            for symbol in ("BTC", "ETH"):
                future = jobs.get(symbol)
                if future is None:
                    continue
                lane_summaries, flat, stop = future.result()
                summaries.extend(lane_summaries)
                if not flat or stop is not None:
                    reason = stop[1] if stop is not None else "safe_stop_flatten_incomplete"
                    self._emit("safe_stop_uncertain", round=round_number, symbol=symbol, reason=reason)
                    return self._finish(
                        plan,
                        "uncertain",
                        reason,
                        summaries,
                        cycles,
                        total_quote,
                        lanes,
                        preflight,
                        execution_started_ms,
                    )
                self._emit("safe_stop_leg_completed", round=round_number, symbol=symbol)
        finally:
            if owns_pool:
                active_pool.shutdown(wait=True)

        positions = {
            symbol: self._observe_position(
                lane.venue,
                round_number=round_number,
                sequence="safe-stop-final",
                symbol=symbol,
                action="safe_stop_final_check",
            )
            for symbol, lane in lanes.items()
        }
        observations = {
            symbol: self._observe_orders(
                lane,
                round_number=round_number,
                sequence="safe-stop-final",
                symbol=symbol,
                action="safe_stop_final_check",
            )
            for symbol, lane in lanes.items()
        }
        flat = all(
            positions[symbol] is not None
            and abs(Decimal(str(positions[symbol]))) <= (plan.btc if symbol == "BTC" else plan.eth).amount_step / 2
            for symbol in ("BTC", "ETH")
        )
        no_orders = all(
            observation is not None and not observation[0] and _row_count(observation[1]) == 0
            for observation in observations.values()
        )
        if not flat or not no_orders:
            self._emit("safe_stop_uncertain", round=round_number, reason="safe_stop_final_boundary_unverified")
            return self._finish(
                plan,
                "uncertain",
                "safe_stop_final_boundary_unverified",
                summaries,
                cycles,
                total_quote,
                lanes,
                preflight,
                execution_started_ms,
            )
        self._emit("safe_stop_verified", round=round_number)
        return self._finish(
            plan,
            "stopped",
            "safe_stop_flattened",
            summaries,
            cycles,
            total_quote,
            lanes,
            preflight,
            execution_started_ms,
        )

    @staticmethod
    def _delay_seconds(selector: DelaySelector | None, round_number: int, fallback: float) -> float:
        seconds = fallback if selector is None else float(selector(round_number))
        if not math.isfinite(seconds) or seconds < 0:
            raise SafetyError("delay selector returned an invalid duration")
        return seconds

    def _refresh_pending_accounting(
        self,
        round_number: int,
        legs: list[dict[str, Any]],
        lanes: Mapping[str, _Lane],
        lane_stops: dict[str, tuple[str, str]],
    ) -> None:
        for leg in legs:
            pending = leg.pop("_pending_fill_reconciliation", None)
            if not isinstance(pending, _PendingFillReconciliation):
                continue
            symbol = pending.request.symbol
            for attempt in range(1, POST_FLAT_ACCOUNTING_ATTEMPTS + 1):
                leg["post_flat_reconciliation_attempts"] = attempt
                self._emit(
                    "accounting_waiting",
                    round=round_number,
                    symbol=symbol,
                    action=leg.get("action"),
                    attempt=attempt,
                    max_attempts=POST_FLAT_ACCOUNTING_ATTEMPTS,
                )
                try:
                    report = lanes[symbol].reconciler.reconcile(pending.request)
                except Exception as exc:  # noqa: BLE001 - bounded read-only retry; never submits an order
                    leg["reason"] = f"fill_reconciliation:{type(exc).__name__.lower()}"
                    if attempt < POST_FLAT_ACCOUNTING_ATTEMPTS:
                        self._emit(
                            "accounting_retry_wait",
                            round=round_number,
                            symbol=symbol,
                            seconds=1,
                            attempt=attempt + 1,
                            max_attempts=POST_FLAT_ACCOUNTING_ATTEMPTS,
                        )
                        self.sleep(1)
                    continue
                _apply_fill_report(leg, report, pending)
                if report.verified or report.status not in RETRYABLE_ACCOUNTING_STATUSES:
                    self._emit(
                        "accounting_wait_completed",
                        round=round_number,
                        symbol=symbol,
                        status=report.status,
                        verified=report.verified,
                    )
                    break
                if attempt < POST_FLAT_ACCOUNTING_ATTEMPTS:
                    self._emit(
                        "accounting_retry_wait",
                        round=round_number,
                        symbol=symbol,
                        seconds=1,
                        attempt=attempt + 1,
                        max_attempts=POST_FLAT_ACCOUNTING_ATTEMPTS,
                    )
                    self.sleep(1)

        for symbol in ("BTC", "ETH"):
            stop = lane_stops.get(symbol)
            if stop is None or stop[0] != "accounting_uncertain":
                continue
            unresolved = [
                leg
                for leg in legs
                if leg.get("symbol") == symbol
                and leg.get("accounting_required") is True
                and leg.get("accounting_verified") is not True
            ]
            if unresolved:
                reason = str(unresolved[0].get("reason") or stop[1])
                lane_stops[symbol] = (
                    "stopped" if _is_hard_terminal(reason) else "accounting_uncertain",
                    reason,
                )
            else:
                del lane_stops[symbol]

    def _prepare_cycle_leverage(
        self,
        plan: BetaVolumePlan,
        opening_notional: Decimal,
        round_number: int,
    ) -> tuple[int, dict[str, str]]:
        available = self._read_with_retry(
            lambda: _available_quote(self.gateway),
            operation="cycle_balance",
            retry_event="cycle_read_retry",
            read="balance",
            round=round_number,
        )
        selected = select_leverage(
            plan.leverage,
            opening_notional,
            available,
            max_auto_leverage=plan.max_auto_leverage,
            margin_buffer=plan.margin_buffer,
        )
        # Leverage is account-level configuration. Keep these private mutations serial on
        # the coordinator client; the independent lane clients remain concurrent for orders.
        states = {
            "BTC": _ensure_lane_leverage(
                self.gateway,
                "BTC",
                plan.btc.position_side,
                selected,
                margin_mode=plan.margin_mode,
                read_leverage=lambda: self._read_with_retry(
                    lambda: self.gateway.leverage("BTC"),
                    operation="leverage_observation",
                    retry_event="cycle_read_retry",
                    read="leverage",
                    symbol="BTC",
                    round=round_number,
                ),
            ),
            "ETH": _ensure_lane_leverage(
                self.gateway,
                "ETH",
                plan.eth.position_side,
                selected,
                margin_mode=plan.margin_mode,
                read_leverage=lambda: self._read_with_retry(
                    lambda: self.gateway.leverage("ETH"),
                    operation="leverage_observation",
                    retry_event="cycle_read_retry",
                    read="leverage",
                    symbol="ETH",
                    round=round_number,
                ),
            ),
        }
        self._emit("cycle_leverage_ready", leverage=selected, btc=states["BTC"], eth=states["ETH"])
        return selected, states

    def _run_pair(
        self,
        pool: ThreadPoolExecutor,
        plan: BetaVolumePlan,
        round_number: int,
        sequence_offset: int,
        specs: Mapping[str, _LegSpec],
        lanes: Mapping[str, _Lane],
    ) -> dict[str, tuple[dict[str, Any], tuple[str, str] | None]]:
        futures = {
            symbol: pool.submit(
                self._execute_leg,
                plan,
                (round_number - 1) * (2 + plan.recovery_attempts * 2) + sequence_offset + index,
                spec,
                lanes[symbol],
                round_number,
                respect_stop=True,
            )
            for index, (symbol, spec) in enumerate(specs.items())
        }
        action = next(iter(specs.values())).action
        started = time.monotonic()
        deadline_seconds = float(plan.timeout_seconds)
        self._emit(
            "pair_waiting",
            round=round_number,
            action=action,
            symbols=tuple(futures),
            active_symbols=tuple(futures),
            completed_symbols=(),
            elapsed_ms=0,
            remaining_ms=int(deadline_seconds * 1000),
        )
        by_future = {future: symbol for symbol, future in futures.items()}
        pending = set(by_future)
        completed: set[str] = set()
        results: dict[str, tuple[dict[str, Any], tuple[str, str] | None]] = {}
        while pending:
            done, pending = wait(
                pending,
                timeout=self.PAIR_HEARTBEAT_SECONDS,
                return_when=FIRST_COMPLETED,
            )
            for future in done:
                symbol = by_future[future]
                results[symbol] = future.result()
                completed.add(symbol)
            elapsed_seconds = time.monotonic() - started
            if pending:
                self._emit(
                    "pair_wait_progress",
                    round=round_number,
                    action=action,
                    symbols=tuple(futures),
                    active_symbols=tuple(symbol for symbol in ("BTC", "ETH") if futures.get(symbol) in pending),
                    completed_symbols=tuple(symbol for symbol in ("BTC", "ETH") if symbol in completed),
                    elapsed_ms=int(elapsed_seconds * 1000),
                    remaining_ms=max(0, int((deadline_seconds - elapsed_seconds) * 1000)),
                )
        self._emit(
            "pair_wait_completed",
            round=round_number,
            action=action,
            completed_symbols=tuple(symbol for symbol in ("BTC", "ETH") if symbol in completed),
        )
        return {symbol: results[symbol] for symbol in ("BTC", "ETH") if symbol in results}

    def _flatten_lane(
        self,
        plan: BetaVolumePlan,
        round_number: int,
        sequence_offset: int,
        leg_plan: PairLegPlan,
        lane: _Lane,
        *,
        respect_stop: bool = False,
    ) -> tuple[list[dict[str, Any]], bool, tuple[str, str] | None]:
        summaries: list[dict[str, Any]] = []
        for attempt in range(1, plan.recovery_attempts + 1):
            position = self._observe_position(
                lane.venue,
                round_number=round_number,
                sequence=f"recovery-{attempt}",
                symbol=leg_plan.symbol,
                action="close",
            )
            if position is None:
                return summaries, False, ("observation_uncertain", "position_observation_unavailable")
            quantity = abs(Decimal(str(position)))
            if quantity <= leg_plan.amount_step / 2:
                return summaries, True, None
            close_plan = replace(leg_plan, quantity=quantity, allocated_quote=Decimal(0))
            spec = _LegSpec(
                close_plan,
                "close",
                leg_plan.closing_side,
                0.0,
                f"{plan.plan_id}-r{round_number:03d}-{leg_plan.symbol.lower()}c{attempt}",
            )
            summary, stop = self._execute_leg(
                plan,
                (round_number - 1) * (4 + plan.recovery_attempts * 2) + sequence_offset + (attempt - 1) * 2,
                spec,
                lane,
                round_number,
                respect_stop=respect_stop,
            )
            summary["recovery_attempt"] = attempt
            summaries.append(summary)
            position = self._observe_position(
                lane.venue,
                round_number=round_number,
                sequence=f"recovery-{attempt}",
                symbol=leg_plan.symbol,
                action="close_check",
            )
            if position is not None and abs(Decimal(str(position))) <= leg_plan.amount_step / 2:
                return summaries, True, stop
            if stop is not None and (_is_uncertain_stop(stop) or _is_hard_terminal(stop[1])):
                return summaries, False, stop
        return summaries, False, ("stopped", "recovery_attempts_exhausted")

    def _execute_leg(
        self,
        plan: BetaVolumePlan,
        sequence: int,
        spec: _LegSpec,
        lane: _Lane,
        round_number: int,
        *,
        respect_stop: bool = True,
    ) -> tuple[dict[str, Any], tuple[str, str] | None]:
        venue = lane.venue
        started_at_ms = self.now_ms()
        self._emit(
            "leg_preparing",
            round=round_number,
            sequence=sequence,
            symbol=spec.plan.symbol,
            action=spec.action,
            side=spec.side,
        )
        start_position = self._observe_position(
            venue,
            round_number=round_number,
            sequence=sequence,
            symbol=spec.plan.symbol,
            action=f"{spec.action}_start",
        )
        if start_position is None:
            reason = "starting_position_unavailable"
            return _leg_exception_summary(sequence, spec, reason), ("observation_uncertain", reason)
        self._emit(
            "leg_started",
            round=round_number,
            sequence=sequence,
            symbol=spec.plan.symbol,
            action=spec.action,
            side=spec.side,
            quantity=decimal_text(spec.plan.quantity),
        )

        def progress_sink(event: Mapping[str, object]) -> None:
            detail = dict(event)
            progress_event = str(detail.pop("event", "unknown"))
            self._emit(
                "leg_progress",
                round=round_number,
                sequence=sequence,
                symbol=spec.plan.symbol,
                action=spec.action,
                side=spec.side,
                progress_event=progress_event,
                **detail,
            )

        try:
            executor_kwargs: dict[str, Any] = {"progress_sink": progress_sink}
            if respect_stop and self._stop_callback_configured:
                executor_kwargs["stop_requested"] = self.stop_requested
            result = execute_adaptive_maker_target(
                venue,
                AdaptiveMakerPolicy(REAL_POLICY),
                TargetRequest(
                    side=spec.side,  # type: ignore[arg-type]
                    target_position=spec.target_position,
                    deadline_ms=plan.timeout_seconds * 1000,
                    poll_interval_ms=250,
                    max_requotes=max(30, plan.timeout_seconds),
                    tolerance_quantity=float(spec.plan.amount_step / 2),
                    client_prefix=spec.client_prefix,
                ),
                **executor_kwargs,
            )
        except ObservationUnavailableError as exc:
            reason = exc.reason
            summary = _leg_exception_summary(sequence, spec, reason)
            self._emit(
                "leg_uncertain",
                round=round_number,
                sequence=sequence,
                symbol=spec.plan.symbol,
                action=spec.action,
                reason=reason,
            )
            return summary, ("observation_uncertain", reason)
        except Exception as exc:  # noqa: BLE001 - a mutation may have landed; never continue to another leg
            reason = f"leg_exception:{type(exc).__name__.lower()}"
            summary = _leg_exception_summary(sequence, spec, reason)
            deadline_reached = self.now_ms() - started_at_ms >= plan.timeout_seconds * 1000
            cleanup = getattr(venue, "cancel_all_and_verify", None)
            if deadline_reached and callable(cleanup):
                self._emit(
                    "leg_progress",
                    round=round_number,
                    sequence=sequence,
                    symbol=spec.plan.symbol,
                    action=spec.action,
                    side=spec.side,
                    progress_event="timeout_cleanup_started",
                )
                try:
                    cleanup_confirmed = bool(cleanup())
                except Exception as cleanup_exc:  # noqa: BLE001 - timeout cleanup must fail closed
                    cleanup_confirmed = False
                    reason = "deadline_cleanup_not_confirmed"
                    self._emit(
                        "leg_progress",
                        round=round_number,
                        sequence=sequence,
                        symbol=spec.plan.symbol,
                        action=spec.action,
                        side=spec.side,
                        progress_event="timeout_cleanup_error",
                        error=type(cleanup_exc).__name__,
                    )
                if not cleanup_confirmed:
                    reason = "deadline_cleanup_not_confirmed"
                    self._emit(
                        "leg_progress",
                        round=round_number,
                        sequence=sequence,
                        symbol=spec.plan.symbol,
                        action=spec.action,
                        side=spec.side,
                        progress_event="timeout_cleanup_not_confirmed",
                    )
                    summary = _leg_exception_summary(sequence, spec, reason)
                    self._emit(
                        "leg_uncertain",
                        round=round_number,
                        sequence=sequence,
                        symbol=spec.plan.symbol,
                        action=spec.action,
                        reason=reason,
                    )
                    return summary, ("submission_uncertain", reason)
                self._emit(
                    "leg_progress",
                    round=round_number,
                    sequence=sequence,
                    symbol=spec.plan.symbol,
                    action=spec.action,
                    side=spec.side,
                    progress_event="timeout_cleanup_confirmed",
                )
            self._emit(
                "leg_stopped" if deadline_reached and callable(cleanup) else "leg_uncertain",
                round=round_number,
                sequence=sequence,
                symbol=spec.plan.symbol,
                action=spec.action,
                reason=reason,
            )
            return summary, ("stopped" if deadline_reached and callable(cleanup) else "submission_uncertain", reason)

        observed_end_position = self._observe_position(
            venue,
            round_number=round_number,
            sequence=sequence,
            symbol=spec.plan.symbol,
            action=f"{spec.action}_check",
        )
        end_position = Decimal(str(result.final_position if observed_end_position is None else observed_end_position))
        executed_quantity = abs(end_position - Decimal(str(start_position)))
        report: LegFillReport | None = None
        fill_request: LegFillRequest | None = None
        reconciliation_error: str | None = None
        order_ids = _submitted_order_ids(result)
        if executed_quantity > spec.plan.amount_step / 2:
            self._emit(
                "leg_waiting",
                round=round_number,
                sequence=sequence,
                symbol=spec.plan.symbol,
                action=spec.action,
                waiting_for="order_identity",
            )
            try:
                filled_history_ids = _history_order_ids(
                    lane.gateway,
                    spec.plan.symbol,
                    spec.client_prefix,
                    started_at_ms,
                    self.now_ms(),
                )
            except Exception as exc:  # noqa: BLE001 - identity recovery is read-only
                if not order_ids:
                    reconciliation_error = f"order_identity_history:{type(exc).__name__.lower()}"
            else:
                if filled_history_ids:
                    order_ids = filled_history_ids
            if not order_ids and reconciliation_error is None:
                reconciliation_error = "missing_order_identity"
        if executed_quantity > spec.plan.amount_step / 2 and order_ids:
            self._emit(
                "leg_waiting",
                round=round_number,
                sequence=sequence,
                symbol=spec.plan.symbol,
                action=spec.action,
                waiting_for="fill_reconciliation",
            )
            try:
                fill_request = LegFillRequest(
                    sequence=sequence,
                    symbol=spec.plan.symbol,
                    action=spec.action,
                    expected_quantity=executed_quantity,
                    tolerance_quantity=spec.plan.amount_step / 2,
                    order_ids=order_ids,
                    started_at_ms=started_at_ms,
                    ended_at_ms=self.now_ms(),
                )
                report = lane.reconciler.reconcile(fill_request)
            except Exception as exc:  # noqa: BLE001 - reconciliation is read-only but completion cannot be assumed
                reconciliation_error = f"fill_reconciliation:{type(exc).__name__.lower()}"

        summary = _leg_summary(sequence, spec, result, report, reconciliation_error, executed_quantity)
        reconciliation_status = reconciliation_error or (report.status if report is not None else None)
        if fill_request is not None and (
            reconciliation_error is not None or reconciliation_status in RETRYABLE_ACCOUNTING_STATUSES
        ):
            summary["_pending_fill_reconciliation"] = _PendingFillReconciliation(
                request=fill_request,
                executor_status=result.status,
                executor_reason=result.reason,
            )
        self._emit(
            "leg_waiting",
            round=round_number,
            sequence=sequence,
            symbol=spec.plan.symbol,
            action=spec.action,
            waiting_for="open_order_clearance",
        )
        observed_orders = self._observe_orders(
            lane,
            round_number=round_number,
            sequence=sequence,
            symbol=spec.plan.symbol,
            action=spec.action,
        )
        if observed_orders is None:
            return summary, ("observation_uncertain", "post_leg_order_observation_unavailable")
        active_orders, trigger_orders = observed_orders
        if active_orders or _row_count(trigger_orders):
            return summary, ("submission_uncertain", "active_order_remains_after_leg")
        if observed_end_position is None:
            return summary, ("observation_uncertain", "ending_position_unavailable")
        if reconciliation_error is not None:
            self._emit(
                "leg_uncertain",
                round=round_number,
                sequence=sequence,
                symbol=spec.plan.symbol,
                action=spec.action,
                reason=reconciliation_error,
            )
            return summary, ("accounting_uncertain", reconciliation_error)
        if executed_quantity > spec.plan.amount_step / 2 and (report is None or not report.verified):
            reason = report.status if report is not None else "missing_order_identity"
            status = "stopped" if _is_hard_terminal(reason) else "accounting_uncertain"
            self._emit(
                "leg_uncertain" if status == "accounting_uncertain" else "leg_stopped",
                round=round_number,
                sequence=sequence,
                symbol=spec.plan.symbol,
                action=spec.action,
                reason=reason,
            )
            return summary, (status, reason)
        if result.status != "completed":
            if result.status == "uncertain" and result.reason in {
                "position_observation_unavailable",
                "market_observation_unavailable",
            }:
                status = "observation_uncertain"
            else:
                status = "submission_uncertain" if result.status == "uncertain" else "stopped"
            self._emit(
                "leg_stopped",
                round=round_number,
                sequence=sequence,
                symbol=spec.plan.symbol,
                action=spec.action,
                reason=result.reason,
            )
            return summary, (status, result.reason)
        self._emit(
            "leg_completed",
            round=round_number,
            sequence=sequence,
            symbol=spec.plan.symbol,
            action=spec.action,
            quote_volume=decimal_text(report.quote_volume if report is not None else Decimal(0)),
            fill_count=report.fill_count if report is not None else 0,
            elapsed_ms=result.elapsed_ms,
            submissions=result.submissions,
            cancels=result.cancels,
        )
        return summary, None

    def _final_acceptance(
        self,
        plan: BetaVolumePlan,
        summaries: list[dict[str, Any]],
        cycles: list[dict[str, Any]],
        total_quote: Decimal,
        lanes: Mapping[str, _Lane],
        preflight: Mapping[str, Any],
        execution_started_ms: int,
    ) -> dict[str, Any]:
        self._emit("final_acceptance_started", total_quote=decimal_text(total_quote))
        positions = {
            symbol: self._observe_position(
                lane.venue,
                round_number=len(cycles),
                sequence="final",
                symbol=symbol,
                action="final_check",
            )
            for symbol, lane in lanes.items()
        }
        flat = all(
            positions[symbol] is not None
            and abs(Decimal(str(positions[symbol]))) <= (plan.btc if symbol == "BTC" else plan.eth).amount_step / 2
            for symbol in ("BTC", "ETH")
        )
        order_observations = {
            symbol: self._observe_orders(
                lane,
                round_number=len(cycles),
                sequence="final",
                symbol=symbol,
                action="final_check",
            )
            for symbol, lane in lanes.items()
        }
        if any(observation is None for observation in order_observations.values()):
            return self._finish(
                plan,
                "uncertain",
                "final_order_observation_unavailable",
                summaries,
                cycles,
                total_quote,
                lanes,
                preflight,
                execution_started_ms,
            )
        no_orders = all(
            not observation[0] and _row_count(observation[1]) == 0
            for observation in order_observations.values()
            if observation is not None
        )
        accounting = _accounting_summary(summaries)
        completed = (
            total_quote >= plan.target_turnover_quote
            and flat
            and no_orders
            and accounting["verified"]
            and accounting["maker_only"]
        )
        self._emit(
            "final_acceptance_completed",
            completed=completed,
            flat=flat,
            no_orders=no_orders,
            accounting_verified=accounting["verified"],
            maker_only=accounting["maker_only"],
        )
        return self._finish(
            plan,
            "completed" if completed else "uncertain",
            "paired_target_completed" if completed else "final_acceptance_invariant_failed",
            summaries,
            cycles,
            total_quote,
            lanes,
            preflight,
            execution_started_ms,
        )

    def _finish(
        self,
        plan: BetaVolumePlan,
        status: str,
        reason: str,
        summaries: list[dict[str, Any]],
        cycles: list[dict[str, Any]],
        total_quote: Decimal,
        lanes: Mapping[str, _Lane],
        preflight: Mapping[str, Any],
        execution_started_ms: int,
    ) -> dict[str, Any]:
        self._emit(
            "workflow_finished",
            status=status,
            reason=reason,
            executed_quote_volume=decimal_text(total_quote),
        )
        payload = _result_payload(
            plan,
            status,
            reason,
            summaries,
            cycles,
            total_quote,
            {symbol: lane.venue for symbol, lane in lanes.items()},
            preflight,
            self.timeline,
            self.now_ms() - execution_started_ms,
        )
        self.store.save(plan, state=status, result=payload)
        return payload

    def _emit(self, event: str, **fields: Any) -> None:
        with self._event_lock:
            row = {
                "event_index": len(self.timeline) + 1,
                "event": event,
                "plan_id": self.current_plan_id,
                "timestamp_ms": self.now_ms(),
                **fields,
            }
            self.timeline.append(row)
            if self.event_sink is None:
                return
            try:
                self.event_sink(row)
            except Exception:  # noqa: BLE001 - presentation/logging must never alter order execution
                return


def select_leverage(
    leverage: str | int,
    opening_notional: Decimal,
    available_quote: Decimal,
    *,
    max_auto_leverage: int = MAX_AUTO_LEVERAGE,
    margin_buffer: Decimal = MARGIN_BUFFER,
) -> int:
    normalized = _normalize_leverage(leverage)
    if not opening_notional.is_finite() or opening_notional <= 0:
        raise ValidationError("opening_notional must be positive and finite")
    if not available_quote.is_finite() or available_quote <= 0:
        raise SafetyError("available USDT is zero or unavailable")
    with localcontext() as context:
        context.prec = 50
        required = int((opening_notional * margin_buffer / available_quote).to_integral_value(rounding=ROUND_CEILING))
    required = max(1, required)
    if normalized == "auto":
        if required > max_auto_leverage:
            raise SafetyError(
                f"this cycle requires {required}x leverage, above the {max_auto_leverage}x automatic limit"
            )
        return required
    fixed = int(normalized)
    if fixed < required:
        raise SafetyError(f"fixed {fixed}x leverage is insufficient; this cycle requires at least {required}x")
    return fixed


def _normalize_leverage(value: object) -> str | int:
    if isinstance(value, str):
        text = value.strip().lower()
        if text == "auto":
            return "auto"
        try:
            parsed = int(text)
        except ValueError:
            raise ValidationError(
                f"leverage must be 'auto' or an integer between 1 and {MAX_FIXED_LEVERAGE}"
            ) from None
    elif isinstance(value, int) and not isinstance(value, bool):
        parsed = value
    else:
        raise ValidationError(f"leverage must be 'auto' or an integer between 1 and {MAX_FIXED_LEVERAGE}")
    if not 1 <= parsed <= MAX_FIXED_LEVERAGE:
        raise ValidationError(f"leverage must be 'auto' or an integer between 1 and {MAX_FIXED_LEVERAGE}")
    return parsed


def _normalize_margin_mode(value: object) -> str:
    normalized = str(value).strip().lower()
    if normalized == "crossed":
        normalized = "cross"
    if normalized not in {"isolated", "cross"}:
        raise ValidationError("margin_mode must be isolated or cross")
    return normalized


def _normalize_direction(value: object) -> str:
    normalized = str(value).strip().lower()
    if normalized not in STRATEGY_DIRECTIONS:
        raise ValidationError("strategy direction is unsupported")
    return normalized


def _direction_sides(direction: str, symbol: str) -> tuple[str, str, str]:
    normal = direction == DEFAULT_STRATEGY_DIRECTION
    is_long = normal if symbol == "BTC" else not normal
    return ("long", "buy", "sell") if is_long else ("short", "sell", "buy")


def _signed_open_quantity(leg: PairLegPlan) -> float:
    quantity = float(leg.quantity)
    return quantity if leg.opening_side == "buy" else -quantity


def _available_quote(gateway: WeexGateway) -> Decimal:
    rows = gateway.account_balance_rows("live")
    usdt = next((row for row in rows if str(row.get("asset") or "").upper() == "USDT"), None)
    if usdt is None:
        raise ValidationError("WEEX balance response has no USDT row")
    return _decimal(usdt.get("availableBalance"), "available USDT")


def _ensure_lane_leverage(
    gateway: WeexGateway,
    symbol: str,
    position_side: str,
    leverage: int,
    *,
    margin_mode: str = "isolated",
    read_leverage: Callable[[], Mapping[str, Any]] | None = None,
) -> str:
    expected_margin_mode = _normalize_margin_mode(margin_mode)
    observe = read_leverage or (lambda: gateway.leverage(symbol))
    try:
        current = observe()
    except Exception as exc:  # noqa: BLE001 - classified without exposing exchange payloads
        raise SafetyError(f"{symbol.lower()}_leverage_read_{type(exc).__name__.lower()}") from exc
    changes: list[str] = []
    if _observed_margin_mode(current) != expected_margin_mode:
        try:
            gateway.configure_margin_mode(symbol, expected_margin_mode)
        except Exception as exc:  # noqa: BLE001 - mutation may have landed; only observe, never resubmit
            try:
                current = observe()
            except Exception:
                current = {}
            if _observed_margin_mode(current) == expected_margin_mode:
                changes.append("margin_updated_after_uncertain_response")
            else:
                raise SafetyError(f"{symbol.lower()}_margin_mode_update_{type(exc).__name__.lower()}") from exc
        else:
            try:
                current = observe()
            except Exception as exc:  # noqa: BLE001 - a successful mutation still requires proof
                raise SafetyError(f"{symbol.lower()}_margin_mode_verify_{type(exc).__name__.lower()}") from exc
            if _observed_margin_mode(current) != expected_margin_mode:
                raise SafetyError(f"{symbol.lower()}_margin_mode_verify_mismatch")
            changes.append("margin_updated")
    if _leverage_matches(current, position_side, leverage, expected_margin_mode):
        return "+".join(changes) if changes else "unchanged"
    try:
        gateway.configure_leverage(symbol, leverage, expected_margin_mode)
    except Exception as exc:  # noqa: BLE001 - mutation may have landed; only observe, never resubmit
        try:
            observed = observe()
        except Exception:
            observed = {}
        if _leverage_matches(observed, position_side, leverage, expected_margin_mode):
            changes.append("leverage_updated_after_uncertain_response")
            return "+".join(changes)
        raise SafetyError(f"{symbol.lower()}_leverage_update_{type(exc).__name__.lower()}") from exc
    try:
        verified = observe()
    except Exception as exc:  # noqa: BLE001 - a successful mutation still requires proof
        raise SafetyError(f"{symbol.lower()}_leverage_verify_{type(exc).__name__.lower()}") from exc
    if not _leverage_matches(verified, position_side, leverage, expected_margin_mode):
        raise SafetyError(f"{symbol.lower()}_leverage_verify_mismatch")
    changes.append("leverage_updated")
    return "+".join(changes)


def _observed_margin_mode(payload: Mapping[str, Any]) -> str:
    observed = str(payload.get("marginMode") or payload.get("marginType") or "").strip().lower()
    return "cross" if observed in {"cross", "crossed"} else observed


def _leverage_matches(
    payload: Mapping[str, Any],
    position_side: str,
    expected: int,
    margin_mode: str = "isolated",
) -> bool:
    normalized_margin = _normalize_margin_mode(margin_mode)
    if _observed_margin_mode(payload) != normalized_margin:
        return False
    keys = (
        ("crossLeverage", "leverage", "longLeverage" if position_side == "long" else "shortLeverage")
        if normalized_margin == "cross"
        else ("longLeverage" if position_side == "long" else "shortLeverage",)
    )
    raw = next((payload.get(key) for key in keys if payload.get(key) is not None), None)
    try:
        actual = Decimal(str(raw))
    except Exception:  # noqa: BLE001 - malformed exchange observation is simply non-matching
        return False
    return actual == Decimal(expected)


def _cycle_leverage_failure_reason(exc: Exception) -> str:
    if isinstance(exc, SafetyError):
        message = str(exc).lower()
        if message and all(character.isalnum() or character == "_" for character in message):
            return message
        if "available usdt" in message or "requires" in message or "automatic limit" in message:
            return "cycle_funding_insufficient"
        return "cycle_leverage_verification_failed"
    return f"cycle_leverage:{type(exc).__name__.lower()}"


def inspect_live_account(
    gateway: WeexGateway,
    required_available: Decimal,
    *,
    opening_notional: Decimal | None = None,
    leverage: str | int = "auto",
    max_auto_leverage: int = MAX_AUTO_LEVERAGE,
    margin_buffer: Decimal = MARGIN_BUFFER,
) -> dict[str, Any]:
    available = _available_quote(gateway)
    active_positions = 0
    regular_orders = 0
    trigger_orders = 0
    position_sizes: dict[str, str | None] = {}
    for symbol in ("BTC", "ETH"):
        position_rows = gateway.positions("live", symbol)
        sizes = [abs(Decimal(summarize_position_size(row))) for row in position_rows]
        active_positions += sum(1 for size in sizes if size > 0)
        position_sizes[symbol] = decimal_text(sum(sizes, Decimal(0)))
        regular_orders += len(gateway.open_orders(symbol, mode="live"))
        trigger_orders += _row_count(gateway.algo_orders(symbol))
    result: dict[str, Any] = {
        "funds_configured": True,
        "available_quote": decimal_text(available),
        "available_sufficient": available >= required_available,
        "active_position_count": active_positions,
        "position_sizes": position_sizes,
        "regular_order_count": regular_orders,
        "trigger_order_count": trigger_orders,
    }
    if opening_notional is not None and available >= required_available:
        result["planned_leverage"] = select_leverage(
            leverage,
            opening_notional,
            available,
            max_auto_leverage=max_auto_leverage,
            margin_buffer=margin_buffer,
        )
    return result


def observed_recovery_quantity(gateway: WeexGateway, symbol: str, position_side: str) -> Decimal:
    rows = gateway.positions("live", symbol)
    quantities = [
        Decimal(summarize_position_size(row))
        for row in rows
        if str(row.get("side") or row.get("positionSide") or "").lower() == position_side.lower()
        and Decimal(summarize_position_size(row)) > 0
    ]
    if len(quantities) > 1:
        raise SafetyError(f"multiple active {symbol} {position_side} positions require manual reconciliation")
    return quantities[0] if quantities else Decimal(0)


def beta_volume_confirmation(plan: BetaVolumePlan) -> str:
    leverage = "AUTO" if plan.leverage == "auto" else f"{plan.leverage}X"
    return f"EXECUTE WEEX LIVE BETA-VOLUME {plan.plan_id.upper()} LEVERAGE_{leverage} POST_ONLY"


def beta_volume_recovery_confirmation(plan: BetaVolumePlan, symbol: str, position_side: str, quantity: Decimal) -> str:
    return (
        f"EXECUTE WEEX LIVE BETA-VOLUME RECOVER {plan.plan_id.upper()} "
        f"{symbol.upper()}_{position_side.upper()} QTY_{decimal_text(quantity)} POST_ONLY"
    )


def _submitted_order_ids(result: TargetExecutionResult) -> tuple[str, ...]:
    seen: set[str] = set()
    order_ids: list[str] = []
    for event in result.events:
        if event.get("event") != "submit":
            continue
        order_id = str(event.get("order_id") or "")
        if order_id and order_id not in seen:
            seen.add(order_id)
            order_ids.append(order_id)
    return tuple(order_ids)


def _history_order_ids(
    gateway: WeexGateway,
    symbol: str,
    client_prefix: str,
    started_at_ms: int,
    ended_at_ms: int,
) -> tuple[str, ...]:
    rows = gateway.order_history(
        "live",
        symbol,
        limit=100,
        start_time=max(0, started_at_ms - 2_000),
        end_time=ended_at_ms,
    )
    prefix = f"{client_prefix}-"
    order_ids: list[str] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        info = row.get("info") if isinstance(row.get("info"), Mapping) else {}
        client_id = str(row.get("clientOrderId") or info.get("clientOrderId") or info.get("newClientOrderId") or "")
        order_id = str(row.get("orderId") or row.get("id") or info.get("orderId") or "")
        executed = row.get("executedQty") or info.get("executedQty") or row.get("filled") or 0
        try:
            has_fill = Decimal(str(executed)) > 0
        except (ArithmeticError, ValueError):
            has_fill = False
        if client_id.startswith(prefix) and order_id and has_fill and order_id not in order_ids:
            order_ids.append(order_id)
    return tuple(order_ids)


def _leg_summary(
    sequence: int,
    spec: _LegSpec,
    result: TargetExecutionResult,
    report: LegFillReport | None,
    reconciliation_error: str | None,
    executed_quantity: Decimal,
) -> dict[str, Any]:
    accounting_required = executed_quantity > spec.plan.amount_step / 2
    verified = report is not None and report.verified
    if result.status != "completed":
        status = result.status
        reason = result.reason
    elif verified:
        status = "completed"
        reason = "authoritative_fill_verified"
    elif accounting_required:
        status = "uncertain"
        reason = reconciliation_error or (report.status if report is not None else "missing_order_identity")
    else:
        status = "completed"
        reason = "no_fill"
    return {
        "sequence": sequence,
        "symbol": spec.plan.symbol,
        "action": spec.action,
        "side": spec.side,
        "position_side": spec.plan.position_side,
        "status": status,
        "reason": reason,
        "verification_status": report.status if report is not None else reconciliation_error or "not_reconciled",
        "accounting_required": accounting_required,
        "accounting_verified": not accounting_required or verified,
        "accounting_source": "user_trades" if report is not None else None,
        "maker_only": report.maker_only if report is not None else False,
        "fill_count": report.fill_count if report is not None else 0,
        "quote_volume": decimal_text(report.quote_volume) if report is not None else "0",
        "executed_quantity": decimal_text(report.executed_quantity if report is not None else executed_quantity),
        "maker_count": report.maker_count if report is not None else 0,
        "taker_count": report.taker_count if report is not None else 0,
        "unknown_liquidity_count": report.unknown_liquidity_count if report is not None else 0,
        "commission_by_asset": (
            {asset: decimal_text(value) for asset, value in sorted(report.commission_by_asset.items())}
            if report is not None
            else {}
        ),
        "realized_pnl": decimal_text(report.realized_pnl) if report is not None else "0",
        "warnings": list(report.warnings) if report is not None else [],
        "elapsed_ms": result.elapsed_ms,
        "submissions": result.submissions,
        "cancels": result.cancels,
        "executor_observation": {
            "fill_count": result.fill_count,
            "quote_volume": decimal_text(Decimal(str(result.quote_volume))),
            "maker_only": result.maker_only,
            "observation_errors": result.observation_errors,
        },
    }


def _apply_fill_report(
    leg: dict[str, Any],
    report: LegFillReport,
    pending: _PendingFillReconciliation,
) -> None:
    verified = report.verified
    if verified and pending.executor_status == "completed":
        status = "completed"
        reason = "authoritative_fill_verified"
    elif verified:
        status = pending.executor_status
        reason = pending.executor_reason
    else:
        status = "stopped" if _is_hard_terminal(report.status) else "uncertain"
        reason = report.status
    leg.update(
        {
            "status": status,
            "reason": reason,
            "verification_status": report.status,
            "accounting_verified": verified,
            "accounting_source": "user_trades",
            "maker_only": report.maker_only,
            "fill_count": report.fill_count,
            "quote_volume": decimal_text(report.quote_volume),
            "executed_quantity": decimal_text(report.executed_quantity),
            "maker_count": report.maker_count,
            "taker_count": report.taker_count,
            "unknown_liquidity_count": report.unknown_liquidity_count,
            "commission_by_asset": {
                asset: decimal_text(value) for asset, value in sorted(report.commission_by_asset.items())
            },
            "realized_pnl": decimal_text(report.realized_pnl),
            "warnings": list(report.warnings),
        }
    )


def _leg_exception_summary(sequence: int, spec: _LegSpec, reason: str) -> dict[str, Any]:
    return {
        "sequence": sequence,
        "symbol": spec.plan.symbol,
        "action": spec.action,
        "side": spec.side,
        "position_side": spec.plan.position_side,
        "status": "uncertain",
        "reason": reason,
        "verification_status": "not_reconciled",
        "accounting_required": False,
        "accounting_verified": True,
        "accounting_source": None,
        "maker_only": False,
        "fill_count": 0,
        "quote_volume": "0",
        "executed_quantity": "0",
        "maker_count": 0,
        "taker_count": 0,
        "unknown_liquidity_count": 0,
        "commission_by_asset": {},
        "realized_pnl": "0",
        "warnings": [],
        "elapsed_ms": 0,
        "submissions": None,
        "cancels": None,
        "executor_observation": None,
    }


def _accounting_summary(legs: list[dict[str, Any]]) -> dict[str, Any]:
    quote = Decimal(0)
    realized_pnl = Decimal(0)
    commission_by_asset: dict[str, Decimal] = defaultdict(Decimal)
    for leg in legs:
        quote += Decimal(str(leg.get("quote_volume") or 0))
        realized_pnl += Decimal(str(leg.get("realized_pnl") or 0))
        commission = leg.get("commission_by_asset")
        if isinstance(commission, Mapping):
            for asset, value in commission.items():
                commission_by_asset[str(asset)] += Decimal(str(value))
    verified = bool(legs) and all(bool(leg.get("accounting_verified")) for leg in legs)
    maker_count = sum(int(leg.get("maker_count") or 0) for leg in legs)
    taker_count = sum(int(leg.get("taker_count") or 0) for leg in legs)
    unknown_count = sum(int(leg.get("unknown_liquidity_count") or 0) for leg in legs)
    fill_count = sum(int(leg.get("fill_count") or 0) for leg in legs)
    return {
        "source": "user_trades",
        "verified": verified,
        "fill_count": fill_count,
        "maker_count": maker_count,
        "taker_count": taker_count,
        "unknown_liquidity_count": unknown_count,
        "maker_only": verified
        and fill_count > 0
        and maker_count == fill_count
        and taker_count == 0
        and unknown_count == 0,
        "executed_quote_volume": decimal_text(quote),
        "commission_by_asset": {asset: decimal_text(value) for asset, value in sorted(commission_by_asset.items())},
        "realized_pnl": decimal_text(realized_pnl),
    }


def _result_payload(
    plan: BetaVolumePlan,
    status: str,
    reason: str,
    legs: list[dict[str, Any]],
    cycles: list[dict[str, Any]],
    total_quote: Decimal,
    venues: Mapping[str, LiveAdaptiveMakerVenue],
    preflight: Mapping[str, Any],
    timeline: list[dict[str, Any]],
    elapsed_ms: int,
) -> dict[str, Any]:
    accounting = _accounting_summary(legs)
    achievement = total_quote / plan.target_turnover_quote * Decimal(100)
    excess = max(Decimal(0), total_quote - plan.target_turnover_quote)
    return {
        "schema_version": plan.schema_version,
        "kind": "beta_volume_execution",
        "mode": "live",
        "strategy": plan.direction,
        "status": status,
        "reason": reason,
        "plan_id": plan.plan_id,
        "maker_only": accounting["maker_only"],
        "executed_quote_volume": decimal_text(total_quote),
        "target_turnover_quote": decimal_text(plan.target_turnover_quote),
        "round_turnover_quote": decimal_text(plan.round_turnover_quote),
        "excess_quote": decimal_text(excess),
        "target_achievement_percent": decimal_text(achievement),
        "elapsed_ms": elapsed_ms,
        "accounting": accounting,
        "legs": legs,
        "cycles": cycles,
        "final_positions": {
            f"BTC_{plan.btc.position_side}".upper(): _safe_position(venues["BTC"]),
            f"ETH_{plan.eth.position_side}".upper(): _safe_position(venues["ETH"]),
        },
        "preflight": dict(preflight),
        "timeline": list(timeline),
        "reconciliation_required": status not in {"completed", "executing"},
        "retry_allowed": False,
        "recovery": "Stop. Inspect positions and orders, then create a separately confirmed pure-Maker flatten plan.",
    }


def _leg_from_dict(payload: Any) -> PairLegPlan:
    if not isinstance(payload, Mapping):
        raise ValidationError("stored Beta leg is invalid")
    return PairLegPlan(
        symbol=str(payload["symbol"]),
        position_side=str(payload["position_side"]),
        opening_side=str(payload["opening_side"]),
        closing_side=str(payload["closing_side"]),
        allocated_quote=Decimal(str(payload["allocated_quote"])),
        reference_price=Decimal(str(payload["reference_price"])),
        quantity=Decimal(str(payload["quantity"])),
        amount_step=Decimal(str(payload["amount_step"])),
        open_client_prefix=str(payload["open_client_order_id"]).removesuffix("-001"),
        close_client_prefix=str(payload["close_client_order_id"]).removesuffix("-001"),
    )


def _size_cycle(
    plan: BetaVolumePlan,
    lanes: Mapping[str, _Lane],
    desired_turnover: Decimal,
) -> tuple[PairLegPlan, PairLegPlan, dict[str, str]]:
    opening_budget = desired_turnover / 2
    btc_quote = opening_budget * plan.allocation.btc_long_weight
    eth_quote = opening_budget * plan.allocation.eth_short_weight
    btc_price = _mid_price(lanes["BTC"].gateway, "BTC")
    eth_price = _mid_price(lanes["ETH"].gateway, "ETH")
    btc_step = lanes["BTC"].gateway.amount_step("BTC")
    eth_step = lanes["ETH"].gateway.amount_step("ETH")
    btc_quantity, eth_quantity, estimated = _choose_pair_quantities_for_gateways(
        lanes["BTC"].gateway,
        lanes["ETH"].gateway,
        desired_turnover,
        btc_quote,
        eth_quote,
        btc_price,
        eth_price,
        btc_step,
        eth_step,
    )
    btc_notional = btc_quantity * btc_price
    eth_notional = eth_quantity * eth_price
    if btc_notional >= plan.max_position_quote or eth_notional >= plan.max_position_quote:
        raise SafetyError("a cycle leg reaches or exceeds max_position_quote")
    btc = replace(
        plan.btc,
        allocated_quote=btc_quote,
        reference_price=btc_price,
        quantity=btc_quantity,
        amount_step=btc_step,
    )
    eth = replace(
        plan.eth,
        allocated_quote=eth_quote,
        reference_price=eth_price,
        quantity=eth_quantity,
        amount_step=eth_step,
    )
    return (
        btc,
        eth,
        {
            "estimated_turnover_quote": decimal_text(estimated) or "0",
            "planned_open_beta": decimal_text(eth_notional / btc_notional) or "0",
            "opening_notional_quote": decimal_text(btc_notional + eth_notional) or "0",
        },
    )


def _is_hard_terminal(reason: str) -> bool:
    return reason in {
        "post_only_rejected",
        "taker_fill_detected",
        "unknown_liquidity",
        "venue_did_not_accept_post_only",
        "policy_would_take_liquidity",
        "target_overfilled",
    }


def _terminal_reason(stops: Mapping[str, tuple[str, str]]) -> str | None:
    for symbol in ("BTC", "ETH"):
        stop = stops.get(symbol)
        if stop is not None and _is_uncertain_stop(stop):
            return stop[1]
    for symbol in ("BTC", "ETH"):
        stop = stops.get(symbol)
        if stop is not None and _is_hard_terminal(stop[1]):
            return stop[1]
    return None


def _is_uncertain_stop(stop: tuple[str, str]) -> bool:
    return stop[0] in {"submission_uncertain", "accounting_uncertain", "observation_uncertain"}


def _mid_price(gateway: WeexGateway, symbol: str) -> Decimal:
    book = gateway.order_book(symbol, 5)
    bids = book.get("bids")
    asks = book.get("asks")
    if not isinstance(bids, list) or not bids or not isinstance(asks, list) or not asks:
        raise ValidationError(f"{symbol} order book is missing bids or asks")
    bid = _decimal(bids[0][0], f"{symbol} bid")
    ask = _decimal(asks[0][0], f"{symbol} ask")
    if bid <= 0 or ask <= bid:
        raise ValidationError(f"{symbol} order book is invalid")
    return (bid + ask) / 2


def _choose_pair_quantities(
    gateway: WeexGateway,
    target: Decimal,
    btc_quote: Decimal,
    eth_quote: Decimal,
    btc_price: Decimal,
    eth_price: Decimal,
    btc_step: Decimal,
    eth_step: Decimal,
) -> tuple[Decimal, Decimal, Decimal]:
    return _choose_pair_quantities_for_gateways(
        gateway,
        gateway,
        target,
        btc_quote,
        eth_quote,
        btc_price,
        eth_price,
        btc_step,
        eth_step,
    )


def _choose_pair_quantities_for_gateways(
    btc_gateway: WeexGateway,
    eth_gateway: WeexGateway,
    target: Decimal,
    btc_quote: Decimal,
    eth_quote: Decimal,
    btc_price: Decimal,
    eth_price: Decimal,
    btc_step: Decimal,
    eth_step: Decimal,
) -> tuple[Decimal, Decimal, Decimal]:
    btc_candidates = _quantity_candidates(btc_gateway, "BTC", btc_quote / btc_price, btc_step)
    eth_candidates = _quantity_candidates(eth_gateway, "ETH", eth_quote / eth_price, eth_step)
    candidates: list[tuple[Decimal, Decimal, Decimal, Decimal]] = []
    for btc_quantity in btc_candidates:
        for eth_quantity in eth_candidates:
            estimated = Decimal(2) * (btc_quantity * btc_price + eth_quantity * eth_price)
            if estimated < target:
                continue
            allocation_error = abs(btc_quantity * btc_price - btc_quote) + abs(eth_quantity * eth_price - eth_quote)
            candidates.append((estimated - target, allocation_error, btc_quantity, eth_quantity))
    if not candidates:
        raise ValidationError("no precision-valid BTC/ETH quantity pair reaches the turnover target")
    overshoot, _, btc_quantity, eth_quantity = min(candidates)
    return btc_quantity, eth_quantity, target + overshoot


def _quantity_candidates(gateway: WeexGateway, symbol: str, desired: Decimal, step: Decimal) -> tuple[Decimal, ...]:
    floor = gateway.amount_to_precision(symbol, desired)
    if floor <= 0 or floor < step:
        floor = gateway.amount_to_precision(symbol, step)
    if floor <= 0 or floor < step:
        raise ValidationError(f"{symbol} quantity is below WEEX minimum precision")
    if floor == desired:
        return (floor,)
    ceiling = gateway.amount_to_precision(symbol, floor + step)
    if ceiling <= floor:
        raise ValidationError(f"{symbol} amount precision could not produce a larger quantity")
    return floor, ceiling


def _decimal(value: Any, name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except Exception as exc:  # noqa: BLE001 - normalize exchange payload validation
        raise ValidationError(f"WEEX {name} is not numeric") from exc
    if not result.is_finite():
        raise ValidationError(f"WEEX {name} is not finite")
    return result


def _row_count(value: Any) -> int:
    if isinstance(value, list):
        return len(value)
    if isinstance(value, Mapping):
        rows = value.get("rows") or value.get("data") or value.get("list") or []
        return len(rows) if isinstance(rows, list) else 0
    return 0


def _safe_position(venue: LiveAdaptiveMakerVenue) -> float | None:
    try:
        return venue.position_quantity()
    except Exception:  # noqa: BLE001 - reporting must preserve the original uncertain state
        return None
