from __future__ import annotations

import hashlib
import json
import os
import time
from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import ROUND_UP, Decimal
from pathlib import Path
from typing import Any, Literal

from weex_cli.adaptive_executor import MakerVenue, TargetExecutionResult, TargetRequest, execute_adaptive_maker_target
from weex_cli.adaptive_maker import AdaptiveMakerPolicy, MakerPolicy
from weex_cli.adaptive_volume import REAL_POLICY
from weex_cli.errors import SafetyError, ValidationError
from weex_cli.execution_reconciliation import LegFillReconciler, LegFillReport, LegFillRequest, LiveLegFillReconciler
from weex_cli.gateway import WeexGateway, summarize_position_size
from weex_cli.live_maker_venue import LiveAdaptiveMakerVenue
from weex_cli.models import decimal_text, decimal_value

PLAN_MAX_AGE_SECONDS = 900
MAX_PLAN_PRICE_DRIFT = Decimal("0.05")
MARGIN_BUFFER = Decimal("1.20")
POSITION_BUFFER = Decimal("1.10")
DEFAULT_PLAN_DIRECTORY = Path("data/live-maker-volume-plans")

PositionSide = Literal["long", "short"]
VenueFactory = Callable[[WeexGateway, str, str], LiveAdaptiveMakerVenue]
EventSink = Callable[[Mapping[str, Any]], None]
Executor = Callable[[MakerVenue, MakerPolicy, TargetRequest], TargetExecutionResult]


@dataclass(frozen=True)
class LiveMakerVolumePlan:
    plan_id: str
    created_at_ms: int
    symbol: str
    target_quote: Decimal
    round_quote: Decimal
    reference_price: Decimal
    amount_step: Decimal
    max_position_quote: Decimal
    timeout_seconds: int
    recovery_attempts: int
    max_empty_rounds: int
    cooldown_seconds: float
    leverage: int

    @classmethod
    def create(
        cls,
        gateway: WeexGateway,
        *,
        symbol: str,
        target_quote: str | Decimal,
        round_quote: str | Decimal,
        timeout_seconds: int,
        recovery_attempts: int = 3,
        max_empty_rounds: int = 3,
        cooldown_seconds: float = 1.0,
        leverage: int = 1,
        now_ms: int | None = None,
    ) -> LiveMakerVolumePlan:
        target = decimal_value(target_quote, name="target_quote")
        per_round = decimal_value(round_quote, name="round_quote")
        assert target is not None and per_round is not None
        normalized_symbol = symbol.strip().upper()
        if not normalized_symbol:
            raise ValidationError("symbol is required")
        if timeout_seconds < 1:
            raise ValidationError("timeout_seconds must be positive")
        if not 1 <= recovery_attempts <= 10:
            raise ValidationError("recovery_attempts must be between 1 and 10")
        if not 0 <= max_empty_rounds <= 20:
            raise ValidationError("max_empty_rounds must be between 0 and 20")
        if not 0 <= cooldown_seconds <= 300:
            raise ValidationError("cooldown_seconds must be between 0 and 300")
        if not 1 <= leverage <= 125:
            raise ValidationError("leverage must be between 1 and 125")
        if per_round > target:
            per_round = target

        price = _mid_price(gateway, normalized_symbol)
        step = gateway.amount_step(normalized_symbol)
        quantity = _quantity_for_turnover(gateway, normalized_symbol, per_round, price)
        max_position = max(per_round / 2, quantity * price) * POSITION_BUFFER
        created = now_ms if now_ms is not None else int(time.time() * 1000)
        identity = "|".join(
            (
                normalized_symbol,
                decimal_text(target) or "0",
                decimal_text(per_round) or "0",
                str(timeout_seconds),
                str(recovery_attempts),
                str(max_empty_rounds),
                str(cooldown_seconds),
                str(leverage),
                str(created),
            )
        )
        plan_id = f"lmv-{hashlib.sha256(identity.encode('ascii')).hexdigest()[:10]}"
        return cls(
            plan_id=plan_id,
            created_at_ms=created,
            symbol=normalized_symbol,
            target_quote=target,
            round_quote=per_round,
            reference_price=price,
            amount_step=step,
            max_position_quote=max_position,
            timeout_seconds=timeout_seconds,
            recovery_attempts=recovery_attempts,
            max_empty_rounds=max_empty_rounds,
            cooldown_seconds=cooldown_seconds,
            leverage=leverage,
        )

    @property
    def required_available_quote(self) -> Decimal:
        return self.max_position_quote / Decimal(self.leverage) * MARGIN_BUFFER

    @property
    def estimated_rounds(self) -> int:
        return int((self.target_quote / self.round_quote).to_integral_value(rounding=ROUND_UP))

    def as_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "created_at_ms": self.created_at_ms,
            "expires_at_ms": self.created_at_ms + PLAN_MAX_AGE_SECONDS * 1000,
            "mode": "live",
            "strategy": "alternating_flat_to_flat",
            "symbol": self.symbol,
            "target_quote": decimal_text(self.target_quote),
            "round_quote": decimal_text(self.round_quote),
            "reference_price": decimal_text(self.reference_price),
            "amount_step": decimal_text(self.amount_step),
            "max_position_quote": decimal_text(self.max_position_quote),
            "required_available_quote": decimal_text(self.required_available_quote),
            "timeout_seconds": self.timeout_seconds,
            "recovery_attempts": self.recovery_attempts,
            "max_empty_rounds": self.max_empty_rounds,
            "cooldown_seconds": self.cooldown_seconds,
            "leverage": self.leverage,
            "estimated_rounds": self.estimated_rounds,
            "time_in_force": "POST_ONLY",
            "volume_source": "userTrades.quoteQty",
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> LiveMakerVolumePlan:
        return cls(
            plan_id=str(payload["plan_id"]),
            created_at_ms=int(payload["created_at_ms"]),
            symbol=str(payload["symbol"]),
            target_quote=Decimal(str(payload["target_quote"])),
            round_quote=Decimal(str(payload["round_quote"])),
            reference_price=Decimal(str(payload["reference_price"])),
            amount_step=Decimal(str(payload["amount_step"])),
            max_position_quote=Decimal(str(payload["max_position_quote"])),
            timeout_seconds=int(payload["timeout_seconds"]),
            recovery_attempts=int(payload["recovery_attempts"]),
            max_empty_rounds=int(payload["max_empty_rounds"]),
            cooldown_seconds=float(payload["cooldown_seconds"]),
            leverage=int(payload["leverage"]),
        )


@dataclass(frozen=True)
class LiveMakerVolumeRecord:
    plan: LiveMakerVolumePlan
    state: str
    result: Any = None


class LiveMakerVolumePlanStore:
    def __init__(self, directory: Path = DEFAULT_PLAN_DIRECTORY) -> None:
        self.directory = directory

    def create(self, plan: LiveMakerVolumePlan) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self._path(plan.plan_id)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        try:
            descriptor = os.open(path, flags, 0o600)
        except FileExistsError:
            raise SafetyError(f"live Maker volume plan already exists: {plan.plan_id}") from None
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(_record_payload(plan, "planned", None), handle, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return path

    def save(self, plan: LiveMakerVolumePlan, *, state: str, result: Any) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self._path(plan.plan_id)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(_record_payload(plan, state, result), indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(temporary, path)
        return path

    def load_record(self, plan_id: str) -> LiveMakerVolumeRecord:
        path = self._path(plan_id)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise ValidationError(f"live Maker volume plan not found: {plan_id}") from None
        except (OSError, json.JSONDecodeError) as exc:
            raise ValidationError(f"cannot read live Maker volume plan: {plan_id}") from exc
        if not isinstance(payload, Mapping) or payload.get("schema_version") != 1:
            raise ValidationError("stored live Maker volume plan is invalid")
        plan_payload = payload.get("plan")
        if not isinstance(plan_payload, Mapping):
            raise ValidationError("stored live Maker volume plan has no plan payload")
        return LiveMakerVolumeRecord(
            plan=LiveMakerVolumePlan.from_dict(plan_payload),
            state=str(payload.get("state") or "unknown"),
            result=payload.get("result"),
        )

    def load(self, plan_id: str) -> LiveMakerVolumePlan:
        return self.load_record(plan_id).plan

    def claim_for_execution(self, plan: LiveMakerVolumePlan) -> None:
        record = self.load_record(plan.plan_id)
        if record.state != "planned":
            raise SafetyError(f"live Maker volume plan is already {record.state}; create a new dry run")
        self.save(plan, state="executing", result={"status": "executing", "reason": "claimed"})

    def _path(self, plan_id: str) -> Path:
        if not plan_id or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789-" for character in plan_id):
            raise ValidationError("invalid live Maker volume plan ID")
        return self.directory / f"{plan_id}.json"


class LiveMakerVolumeService:
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
        current_price = _mid_price(self.gateway, plan.symbol)
        drift = abs(current_price - plan.reference_price) / plan.reference_price
        if drift > MAX_PLAN_PRICE_DRIFT:
            raise SafetyError("market moved more than 5% since planning; create a new dry run")
        if self.gateway.amount_step(plan.symbol) != plan.amount_step:
            raise SafetyError("market amount precision changed since planning; create a new dry run")
        positions = _active_positions(self.gateway, plan.symbol)
        regular_orders = self.gateway.open_orders(plan.symbol, mode="live")
        trigger_orders = _row_count(self.gateway.algo_orders(plan.symbol))
        if positions or regular_orders or trigger_orders:
            raise SafetyError("symbol has positions or orders; refusing to start a new volume session")
        available = _available_quote(self.gateway)
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

    def _execute_round(self, round_number: int, desired_quote: Decimal) -> dict[str, Any]:
        assert self.plan is not None
        position_side: PositionSide = "long" if round_number % 2 else "short"
        venue = self.venue_factory(self.gateway, self.plan.symbol, position_side)
        snapshot = venue.snapshot()
        quantity = _quantity_for_turnover(
            self.gateway,
            self.plan.symbol,
            desired_quote,
            Decimal(str(snapshot.mid)),
        )
        opening_notional = quantity * Decimal(str(snapshot.ask if position_side == "long" else snapshot.bid))
        if opening_notional >= self.plan.max_position_quote:
            return _round_outcome(round_number, position_side, "stopped", "position_limit_reached", terminal=True)

        open_side = "buy" if position_side == "long" else "sell"
        target = float(quantity) if position_side == "long" else -float(quantity)
        self._emit(
            "volume_round_started",
            round=round_number,
            position_side=position_side,
            desired_quote=decimal_text(desired_quote),
            quantity=decimal_text(quantity),
        )
        opening = self._execute_leg(
            round_number=round_number,
            attempt=1,
            action="open",
            side=open_side,
            target_position=target,
            venue=venue,
            client_prefix=f"{self.plan.plan_id}-r{round_number:03d}-o",
        )
        legs = [opening]
        position = _safe_position(venue)
        if opening["execution_uncertain"] or position is None:
            return _round_outcome(
                round_number,
                position_side,
                "uncertain",
                opening["reason"],
                legs=legs,
                terminal=True,
                uncertain=True,
            )

        opened = abs(Decimal(str(position))) > self.plan.amount_step / 2
        if opened:
            close_legs, flat, close_reason, close_uncertain = self._flatten_round(
                round_number,
                position_side,
                venue,
            )
            legs.extend(close_legs)
            if not flat:
                return _round_outcome(
                    round_number,
                    position_side,
                    "uncertain" if close_uncertain else "stopped",
                    close_reason,
                    legs=legs,
                    terminal=True,
                    uncertain=close_uncertain,
                )

        close_problem = next(
            (
                leg
                for leg in legs[1:]
                if leg["accounting_uncertain"] or leg["taker_or_unknown"] or leg["reason"] == "post_only_rejected"
            ),
            None,
        )
        if close_problem is not None:
            accounting_uncertain = bool(close_problem["accounting_uncertain"])
            return _round_outcome(
                round_number,
                position_side,
                "uncertain" if accounting_uncertain else "stopped",
                str(close_problem["reason"]),
                legs=legs,
                terminal=True,
                uncertain=accounting_uncertain,
                flat=True,
            )

        if opening["taker_or_unknown"]:
            return _round_outcome(
                round_number,
                position_side,
                "stopped",
                opening["reason"],
                legs=legs,
                terminal=True,
                flat=True,
            )
        if opening["accounting_uncertain"]:
            return _round_outcome(
                round_number,
                position_side,
                "uncertain",
                str(opening["reason"]),
                legs=legs,
                terminal=True,
                uncertain=True,
                flat=True,
            )
        if opening["reason"] == "post_only_rejected":
            return _round_outcome(
                round_number,
                position_side,
                "stopped",
                "post_only_rejected",
                legs=legs,
                terminal=True,
                flat=not opened or _is_flat(venue, self.plan.amount_step),
            )
        if not opened:
            return _round_outcome(
                round_number,
                position_side,
                "empty",
                opening["reason"],
                legs=legs,
                terminal=False,
                flat=True,
            )
        if not all(bool(leg["verified_maker"]) for leg in legs if leg["executed_quantity"] != "0"):
            return _round_outcome(
                round_number,
                position_side,
                "uncertain",
                "fill_verification_incomplete",
                legs=legs,
                terminal=True,
                uncertain=True,
                flat=True,
            )
        status = "completed" if opening["status"] == "completed" else "recovered"
        return _round_outcome(
            round_number,
            position_side,
            status,
            "round_flat" if status == "completed" else "partial_open_recovered_flat",
            legs=legs,
            terminal=False,
            flat=True,
        )

    def _flatten_round(
        self,
        round_number: int,
        position_side: PositionSide,
        venue: LiveAdaptiveMakerVenue,
    ) -> tuple[list[dict[str, Any]], bool, str, bool]:
        assert self.plan is not None
        legs: list[dict[str, Any]] = []
        close_side = "sell" if position_side == "long" else "buy"
        for attempt in range(1, self.plan.recovery_attempts + 1):
            if _is_flat(venue, self.plan.amount_step):
                return legs, True, "position_flat", False
            try:
                active = self.gateway.open_orders(self.plan.symbol, mode="live")
            except Exception as exc:  # noqa: BLE001 - never submit while open-order state is unknown
                return legs, False, f"close_preflight:{type(exc).__name__.lower()}", True
            if active:
                return legs, False, "active_order_remains_before_close", True
            leg = self._execute_leg(
                round_number=round_number,
                attempt=attempt,
                action="close",
                side=close_side,
                target_position=0.0,
                venue=venue,
                client_prefix=f"{self.plan.plan_id}-r{round_number:03d}-c{attempt}",
            )
            legs.append(leg)
            if leg["execution_uncertain"]:
                return legs, False, str(leg["reason"]), True
            if leg["accounting_uncertain"]:
                if _is_flat(venue, self.plan.amount_step):
                    return legs, True, str(leg["reason"]), True
                continue
            if leg["taker_or_unknown"]:
                if _is_flat(venue, self.plan.amount_step):
                    return legs, True, str(leg["reason"]), False
                continue
            if leg["reason"] == "post_only_rejected":
                return legs, _is_flat(venue, self.plan.amount_step), "post_only_rejected_during_close", False
            if _is_flat(venue, self.plan.amount_step):
                return legs, True, "position_flat", False
        return legs, False, "maker_flatten_attempts_exhausted", False

    def _execute_leg(
        self,
        *,
        round_number: int,
        attempt: int,
        action: str,
        side: str,
        target_position: float,
        venue: LiveAdaptiveMakerVenue,
        client_prefix: str,
    ) -> dict[str, Any]:
        assert self.plan is not None
        started_at_ms = self.now_ms()
        start_position = _safe_position(venue)
        if start_position is None:
            return _leg_error(action, attempt, "starting_position_unavailable", uncertain=True)
        self._emit(
            "volume_leg_started",
            round=round_number,
            attempt=attempt,
            action=action,
            side=side,
            start_position=start_position,
            target_position=target_position,
        )
        try:
            result = self.executor(
                venue,
                AdaptiveMakerPolicy(REAL_POLICY),
                TargetRequest(
                    side=side,  # type: ignore[arg-type]
                    target_position=target_position,
                    deadline_ms=self.plan.timeout_seconds * 1000,
                    poll_interval_ms=250,
                    max_requotes=30,
                    tolerance_quantity=float(self.plan.amount_step / 2),
                    client_prefix=client_prefix,
                ),
            )
        except Exception as exc:  # noqa: BLE001 - a submission may have landed; never continue automatically
            reason = f"leg_exception:{type(exc).__name__.lower()}"
            self._emit("volume_leg_uncertain", round=round_number, action=action, reason=reason)
            return _leg_error(action, attempt, reason, uncertain=True)

        end_position = Decimal(str(result.final_position))
        executed_quantity = abs(end_position - Decimal(str(result.start_position)))
        report: LegFillReport | None = None
        reconciliation_error: str | None = None
        order_ids = _submitted_order_ids(result)
        if executed_quantity > self.plan.amount_step / 2:
            if not order_ids:
                reconciliation_error = "missing_order_identity"
            else:
                try:
                    report = self.fill_reconciler.reconcile(
                        LegFillRequest(
                            sequence=round_number,
                            symbol=self.plan.symbol,
                            action=action,
                            expected_quantity=executed_quantity,
                            tolerance_quantity=self.plan.amount_step / 2,
                            order_ids=order_ids,
                            started_at_ms=started_at_ms,
                            ended_at_ms=self.now_ms(),
                        )
                    )
                except Exception as exc:  # noqa: BLE001 - completion requires authoritative fills
                    reconciliation_error = f"fill_reconciliation:{type(exc).__name__.lower()}"

        verified_maker = report is not None and report.verified and report.maker_only
        taker_or_unknown = bool(report is not None and (report.taker_count or report.unknown_liquidity_count))
        if report is not None:
            self._record_report(report)
        reason = reconciliation_error or (
            report.status if report is not None and not report.verified else result.reason
        )
        execution_uncertain = result.status == "uncertain"
        accounting_uncertain = reconciliation_error is not None
        if executed_quantity > self.plan.amount_step / 2 and (report is None or not report.verified):
            accounting_uncertain = accounting_uncertain or not taker_or_unknown
        uncertain = execution_uncertain or accounting_uncertain
        summary = {
            "action": action,
            "attempt": attempt,
            "status": result.status,
            "reason": reason,
            "executed_quantity": decimal_text(executed_quantity),
            "quote_volume": decimal_text(report.quote_volume if report is not None and report.verified else Decimal(0)),
            "fill_count": report.fill_count if report is not None and report.verified else 0,
            "maker_count": report.maker_count if report is not None else 0,
            "taker_count": report.taker_count if report is not None else 0,
            "unknown_liquidity_count": report.unknown_liquidity_count if report is not None else 0,
            "verified_maker": verified_maker,
            "taker_or_unknown": taker_or_unknown,
            "uncertain": uncertain,
            "execution_uncertain": execution_uncertain,
            "accounting_uncertain": accounting_uncertain,
            "elapsed_ms": result.elapsed_ms,
            "submissions": result.submissions,
            "cancels": result.cancels,
            "requotes": result.requotes,
            "post_only_rejections": result.post_only_rejections,
        }
        event = "volume_leg_completed" if result.status == "completed" and not uncertain else "volume_leg_stopped"
        self._emit(
            event,
            round=round_number,
            attempt=attempt,
            action=action,
            status=result.status,
            reason=reason,
            quote_volume=summary["quote_volume"],
            total_verified_quote=decimal_text(self.verified_quote),
        )
        return summary

    def _record_report(self, report: LegFillReport) -> None:
        if report.verified and report.maker_only:
            self.verified_quote += report.quote_volume
        self.fill_count += report.fill_count
        self.maker_count += report.maker_count
        self.taker_count += report.taker_count
        self.unknown_liquidity_count += report.unknown_liquidity_count
        self.realized_pnl += report.realized_pnl
        for asset, amount in report.commission_by_asset.items():
            self.commission_by_asset[asset] += amount

    def _final_acceptance(self) -> dict[str, Any]:
        assert self.plan is not None
        try:
            flat = not _active_positions(self.gateway, self.plan.symbol)
            no_regular = not self.gateway.open_orders(self.plan.symbol, mode="live")
            no_triggers = _row_count(self.gateway.algo_orders(self.plan.symbol)) == 0
        except Exception as exc:  # noqa: BLE001 - final state must be observed, never assumed
            return self._finish("uncertain", f"final_observation:{type(exc).__name__.lower()}")
        completed = (
            self.verified_quote >= self.plan.target_quote
            and flat
            and no_regular
            and no_triggers
            and self.taker_count == 0
            and self.unknown_liquidity_count == 0
        )
        return self._finish(
            "completed" if completed else "uncertain",
            "maker_volume_target_completed" if completed else "final_acceptance_invariant_failed",
        )

    def _finish(self, status: str, reason: str) -> dict[str, Any]:
        assert self.plan is not None
        reconciliation_required = status == "uncertain"
        payload = self._result(status, reason, reconciliation_required=reconciliation_required)
        self._emit(
            "volume_workflow_finished",
            status=status,
            reason=reason,
            verified_quote=decimal_text(self.verified_quote),
        )
        payload["timeline"] = list(self.timeline)
        self.store.save(self.plan, state=status, result=payload)
        return payload

    def _result(self, status: str, reason: str, *, reconciliation_required: bool) -> dict[str, Any]:
        assert self.plan is not None
        remaining = max(Decimal(0), self.plan.target_quote - self.verified_quote)
        excess = max(Decimal(0), self.verified_quote - self.plan.target_quote)
        return {
            "schema_version": 1,
            "kind": "live_maker_volume_execution",
            "mode": "live",
            "status": status,
            "reason": reason,
            "plan_id": self.plan.plan_id,
            "symbol": self.plan.symbol,
            "target_quote": decimal_text(self.plan.target_quote),
            "verified_quote": decimal_text(self.verified_quote),
            "remaining_quote": decimal_text(remaining),
            "excess_quote": decimal_text(excess),
            "achievement_percent": decimal_text(self.verified_quote / self.plan.target_quote * 100),
            "rounds_completed": sum(1 for row in self.rounds if row.get("flat")),
            "rounds_attempted": len(self.rounds),
            "fill_count": self.fill_count,
            "maker_count": self.maker_count,
            "taker_count": self.taker_count,
            "unknown_liquidity_count": self.unknown_liquidity_count,
            "maker_only": self.fill_count > 0 and self.taker_count == 0 and self.unknown_liquidity_count == 0,
            "commission_by_asset": {
                asset: decimal_text(amount) for asset, amount in sorted(self.commission_by_asset.items())
            },
            "realized_pnl": decimal_text(self.realized_pnl),
            "elapsed_ms": max(0, self.now_ms() - self.started_at_ms),
            "rounds": list(self.rounds),
            "reconciliation_required": reconciliation_required,
            "retry_allowed": False,
            "recovery": (
                "Inspect live positions and active orders before creating a new plan."
                if reconciliation_required
                else None
            ),
            "timeline": list(self.timeline),
        }

    def _checkpoint(self, state: str, reason: str, **fields: Any) -> None:
        assert self.plan is not None
        payload = self._result(state, reason, reconciliation_required=False)
        payload.update(fields)
        self.store.save(self.plan, state=state, result=payload)

    def _reset(self, plan: LiveMakerVolumePlan) -> None:
        self.plan = plan
        self.timeline = []
        self.rounds = []
        self.verified_quote = Decimal(0)
        self.maker_count = 0
        self.taker_count = 0
        self.unknown_liquidity_count = 0
        self.fill_count = 0
        self.commission_by_asset = defaultdict(Decimal)
        self.realized_pnl = Decimal(0)
        self.started_at_ms = self.now_ms()

    def _emit(self, event: str, **fields: Any) -> None:
        row = {
            "event_index": len(self.timeline) + 1,
            "event": event,
            "plan_id": self.plan.plan_id if self.plan is not None else None,
            "timestamp_ms": self.now_ms(),
            **fields,
        }
        self.timeline.append(row)
        if self.event_sink is None:
            return
        try:
            self.event_sink(row)
        except Exception:  # noqa: BLE001 - progress presentation must not alter execution
            return


def live_maker_volume_confirmation(plan: LiveMakerVolumePlan) -> str:
    return " ".join(
        (
            "EXECUTE WEEX LIVE MAKER VOLUME",
            plan.symbol,
            f"TARGET_{decimal_text(plan.target_quote)}",
            f"ROUND_{decimal_text(plan.round_quote)}",
            f"LEVERAGE_{plan.leverage}",
            f"TIMEOUT_{plan.timeout_seconds}",
            f"RECOVERY_{plan.recovery_attempts}",
            f"EMPTY_{plan.max_empty_rounds}",
            "POST_ONLY",
            plan.plan_id.upper(),
        )
    )


def plan_payload(plan: LiveMakerVolumePlan, path: Path) -> dict[str, Any]:
    return {
        "status": "dry_run",
        "kind": "live_maker_volume_plan",
        "action": "live_maker_volume",
        "mode": "live",
        "plan": plan.as_dict(),
        "plan_file": str(path),
        "confirm": live_maker_volume_confirmation(plan),
    }


def _record_payload(plan: LiveMakerVolumePlan, state: str, result: Any) -> dict[str, Any]:
    return {"schema_version": 1, "state": state, "plan": plan.as_dict(), "result": result}


def _quantity_for_turnover(gateway: WeexGateway, symbol: str, turnover: Decimal, price: Decimal) -> Decimal:
    step = gateway.amount_step(symbol)
    raw = turnover / (Decimal(2) * price)
    lower = gateway.amount_to_precision(symbol, raw)
    upper_raw = (raw / step).to_integral_value(rounding=ROUND_UP) * step
    upper = gateway.amount_to_precision(symbol, upper_raw)
    candidates = [quantity for quantity in {lower, upper, step} if quantity > 0]
    if not candidates:
        raise ValidationError("round turnover is below the market minimum quantity")
    return min(candidates, key=lambda quantity: (abs(Decimal(2) * quantity * price - turnover), quantity))


def _mid_price(gateway: WeexGateway, symbol: str) -> Decimal:
    book = gateway.order_book(symbol, 5)
    bids = book.get("bids")
    asks = book.get("asks")
    if not isinstance(bids, list) or not bids or not isinstance(asks, list) or not asks:
        raise ValidationError(f"{symbol} order book is unavailable")
    bid = Decimal(str(bids[0][0]))
    ask = Decimal(str(asks[0][0]))
    if bid <= 0 or ask <= bid:
        raise ValidationError(f"{symbol} order book is invalid")
    return (bid + ask) / 2


def _available_quote(gateway: WeexGateway) -> Decimal:
    row = next(
        (
            item
            for item in gateway.account_balance_rows("live")
            if str(item.get("asset") or "").strip().upper() == "USDT"
        ),
        None,
    )
    if row is None:
        raise ValidationError("WEEX balance response has no USDT row")
    for key in ("availableBalance", "available", "free"):
        if row.get(key) not in (None, ""):
            value = Decimal(str(row[key]))
            if value.is_finite() and value >= 0:
                return value
    raise ValidationError("WEEX balance response has no available USDT value")


def _active_positions(gateway: WeexGateway, symbol: str) -> list[Mapping[str, Any]]:
    return [
        row
        for row in gateway.positions("live", symbol)
        if isinstance(row, Mapping) and Decimal(summarize_position_size(row)) > 0
    ]


def _submitted_order_ids(result: TargetExecutionResult) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            str(event.get("order_id"))
            for event in result.events
            if event.get("event") == "submit" and event.get("order_id")
        )
    )


def _safe_position(venue: LiveAdaptiveMakerVenue) -> float | None:
    try:
        return venue.position_quantity()
    except Exception:  # noqa: BLE001 - uncertainty is represented explicitly
        return None


def _is_flat(venue: LiveAdaptiveMakerVenue, amount_step: Decimal) -> bool:
    position = _safe_position(venue)
    return position is not None and abs(Decimal(str(position))) <= amount_step / 2


def _row_count(payload: Any) -> int:
    if isinstance(payload, list):
        return len(payload)
    if isinstance(payload, Mapping):
        rows = payload.get("rows") or payload.get("data") or payload.get("list")
        return len(rows) if isinstance(rows, list) else 0
    return 0


def _leg_error(action: str, attempt: int, reason: str, *, uncertain: bool) -> dict[str, Any]:
    return {
        "action": action,
        "attempt": attempt,
        "status": "uncertain" if uncertain else "failed",
        "reason": reason,
        "executed_quantity": "0",
        "quote_volume": "0",
        "fill_count": 0,
        "maker_count": 0,
        "taker_count": 0,
        "unknown_liquidity_count": 0,
        "verified_maker": False,
        "taker_or_unknown": False,
        "uncertain": uncertain,
        "execution_uncertain": uncertain,
        "accounting_uncertain": False,
        "elapsed_ms": 0,
        "submissions": 0,
        "cancels": 0,
        "requotes": 0,
        "post_only_rejections": 0,
    }


def _round_outcome(
    number: int,
    position_side: str,
    status: str,
    reason: str,
    *,
    legs: list[dict[str, Any]] | None = None,
    terminal: bool,
    uncertain: bool = False,
    flat: bool = False,
) -> dict[str, Any]:
    rows = legs or []
    return {
        "round": number,
        "position_side": position_side,
        "status": status,
        "reason": reason,
        "quote_volume": decimal_text(sum((Decimal(str(row.get("quote_volume") or 0)) for row in rows), Decimal(0))),
        "fill_count": sum(int(row.get("fill_count") or 0) for row in rows),
        "flat": flat,
        "terminal": terminal,
        "uncertain": uncertain,
        "legs": rows,
    }
