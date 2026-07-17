from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import ccxt

from weex_cli.errors import SubmissionUncertainError, ValidationError
from weex_cli.gateway import WeexGateway, summarize_position_size
from weex_cli.models import OrderIntent, decimal_text, decimal_value
from weex_cli.redaction import redact_text
from weex_cli.service import TradingService
from weex_cli.symbols import base_asset

VOLUME_BUFFER = Decimal("1.01")
MAX_READ_ERRORS = 3
MIN_SUBMIT_INTERVAL = 10.1
TERMINAL_FAILURES = {"CANCELED", "CANCELLED", "EXPIRED", "REJECTED"}


@dataclass(frozen=True)
class MakerVolumePlan:
    symbol: str
    target_quote: Decimal
    fills: int
    max_position_quote: Decimal
    timeout_seconds: int
    poll_interval_seconds: float = 1.0

    @classmethod
    def create(
        cls,
        *,
        symbol: str,
        target_quote: str | Decimal,
        fills: int,
        max_position_quote: str | Decimal,
        timeout_seconds: int,
        poll_interval_seconds: float = 1.0,
    ) -> MakerVolumePlan:
        target = decimal_value(target_quote, name="target_quote")
        max_position = decimal_value(max_position_quote, name="max_position_quote")
        assert target is not None and max_position is not None
        if fills < 2 or fills % 2:
            raise ValidationError("fills must be an even integer of at least 2 so the batch ends flat")
        if timeout_seconds < 1:
            raise ValidationError("timeout_seconds must be at least 1")
        if not 0.2 <= poll_interval_seconds <= 10:
            raise ValidationError("poll_interval_seconds must be between 0.2 and 10")
        if target / fills * VOLUME_BUFFER >= max_position:
            raise ValidationError("target is infeasible for the fill count and max position with the safety buffer")
        return cls(
            symbol=base_asset(symbol),
            target_quote=target,
            fills=fills,
            max_position_quote=max_position,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "mode": "demo",
            "symbol": self.symbol,
            "target_quote_volume": decimal_text(self.target_quote),
            "fills": self.fills,
            "cycles": self.fills // 2,
            "max_position_quote": decimal_text(self.max_position_quote),
            "timeout_seconds_per_order": self.timeout_seconds,
            "poll_interval_seconds": self.poll_interval_seconds,
            "volume_buffer_percent": "1",
        }


def maker_volume_confirmation(plan: MakerVolumePlan) -> str:
    return " ".join(
        [
            "EXECUTE",
            "WEEX",
            "DEMO",
            "MAKER",
            "VOLUME",
            plan.symbol,
            f"TARGET_{decimal_text(plan.target_quote)}",
            f"FILLS_{plan.fills}",
            f"MAX_POSITION_{decimal_text(plan.max_position_quote)}",
            f"TIMEOUT_{plan.timeout_seconds}",
        ]
    )


@dataclass
class _BatchProgress:
    plan: MakerVolumePlan
    prefix: str
    started: float
    attempts: list[dict[str, Any]] = field(default_factory=list)
    fills: list[dict[str, Any]] = field(default_factory=list)
    total_quote: Decimal = Decimal("0")
    opening_quote: Decimal = Decimal("0")
    closing_quote: Decimal = Decimal("0")
    open_quantity: Decimal | None = None
    last_submit_at: float | None = None

    def record_fill(self, action: str, outcome: dict[str, Any]) -> None:
        quote = Decimal(str(outcome["quote_volume"]))
        self.total_quote += quote
        if action == "open":
            self.opening_quote += quote
        else:
            self.closing_quote += quote
            self.open_quantity = None
        excluded = {"batch_status", "reason", "position"}
        self.fills.append({key: value for key, value in outcome.items() if key not in excluded})


class MakerVolumeService:
    def __init__(
        self,
        gateway: WeexGateway,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        prefix_factory: Callable[[str], str] | None = None,
    ) -> None:
        self.gateway = gateway
        self.trading = TradingService(gateway)
        self.clock = clock
        self.sleep = sleep
        self.prefix_factory = prefix_factory or _client_prefix

    def run(self, plan: MakerVolumePlan) -> dict[str, Any]:
        progress = _BatchProgress(plan=plan, prefix=self.prefix_factory(plan.symbol), started=self.clock())
        initial_state = self._position_state(plan.symbol)
        if initial_state["active"]:
            return self._finish(progress, "stopped", "starting_position_not_flat", initial_state)

        for sequence in range(1, plan.fills + 1):
            stopped = self._execute_sequence(progress, sequence)
            if stopped is not None:
                return stopped

        final_state = self._position_state(plan.symbol)
        completed = progress.total_quote >= plan.target_quote and not final_state["active"]
        status = "completed" if completed else "stopped"
        reason = "target_reached" if completed else "target_not_reached_or_position_not_flat"
        return self._finish(progress, status, reason, final_state)

    def _execute_sequence(self, progress: _BatchProgress, sequence: int) -> dict[str, Any] | None:
        plan = progress.plan
        action = "open" if sequence % 2 else "close"
        self._throttle(progress.last_submit_at)
        state = self._position_state(plan.symbol)
        state_error = self._pre_submit_state_error(action, state, progress.open_quantity)
        if state_error:
            return self._finish(progress, "stopped", state_error, state)

        intent, attempt = self._prepare_order(progress, sequence, action)
        progress.attempts.append(attempt)
        submitted_at = self.clock()
        progress.last_submit_at = submitted_at
        submission_error = self._submit_once(progress, intent, attempt)
        if submission_error is not None:
            return submission_error

        outcome = self._wait_for_fill(plan, intent, action, submitted_at)
        attempt.update(outcome)
        if outcome["status"] != "FILLED":
            return self._finish(
                progress,
                str(outcome["batch_status"]),
                str(outcome["reason"]),
                outcome["position"],
            )
        progress.record_fill(action, outcome)
        return None

    def _prepare_order(
        self, progress: _BatchProgress, sequence: int, action: str
    ) -> tuple[OrderIntent, dict[str, Any]]:
        plan = progress.plan
        side = "buy" if action == "open" else "sell"
        price = _best_maker_price(self.gateway.order_book(plan.symbol, 1), side)
        if action == "open":
            remaining_target = max(Decimal("0"), plan.target_quote - progress.total_quote)
            remaining_fills = plan.fills - len(progress.fills)
            desired_quote = remaining_target / remaining_fills * VOLUME_BUFFER
            quantity = self.gateway.amount_to_precision(plan.symbol, desired_quote / price)
            if quantity <= 0:
                raise ValidationError("calculated quantity is below the exchange amount precision")
            if quantity * price >= plan.max_position_quote:
                raise ValidationError("calculated opening notional would reach or exceed max_position_quote")
            progress.open_quantity = quantity
        else:
            assert progress.open_quantity is not None
            quantity = progress.open_quantity

        client_order_id = f"{progress.prefix}-{sequence:02d}-{'o' if action == 'open' else 'c'}"
        intent = OrderIntent.create(
            mode="demo",
            symbol=plan.symbol,
            side=side,
            position_side="long",
            order_type="limit",
            quantity=quantity,
            price=price,
            time_in_force="POST_ONLY",
            client_order_id=client_order_id,
            reduce_only=action == "close",
        )
        attempt = {
            "sequence": sequence,
            "action": action,
            "client_order_id": client_order_id,
            "side": side.upper(),
            "price": decimal_text(price),
            "quantity": decimal_text(quantity),
            "planned_quote": decimal_text(quantity * price),
            "status": "submitting",
        }
        return intent, attempt

    def _submit_once(
        self, progress: _BatchProgress, intent: OrderIntent, attempt: dict[str, Any]
    ) -> dict[str, Any] | None:
        try:
            submission = self.trading.submit_order(intent, allow_existing=True)
        except SubmissionUncertainError as exc:
            attempt.update(status="uncertain", error=redact_text(exc))
            return self._finish(
                progress,
                "uncertain",
                "submission_outcome_uncertain",
                self._safe_position_state(progress.plan.symbol),
            )
        except ccxt.BaseError as exc:
            attempt.update(status="rejected", error=redact_text(exc))
            return self._finish(
                progress,
                "stopped",
                "submission_rejected",
                self._safe_position_state(progress.plan.symbol),
            )

        result = submission.get("result") or submission.get("order") or {}
        if not isinstance(result, dict) or result.get("success") is not False:
            return None
        attempt.update(
            status="rejected",
            error_code=result.get("errorCode"),
            error_message=redact_text(result.get("errorMessage")),
        )
        return self._finish(
            progress,
            "stopped",
            "submission_rejected",
            self._safe_position_state(progress.plan.symbol),
        )

    def _throttle(self, last_submit_at: float | None) -> None:
        if last_submit_at is None:
            return
        remaining_interval = MIN_SUBMIT_INTERVAL - (self.clock() - last_submit_at)
        if remaining_interval > 0:
            self.sleep(remaining_interval)

    def _wait_for_fill(
        self,
        plan: MakerVolumePlan,
        intent: OrderIntent,
        action: str,
        submitted_at: float,
    ) -> dict[str, Any]:
        deadline = submitted_at + plan.timeout_seconds
        read_errors = 0
        last_order: dict[str, Any] | None = None
        while True:
            try:
                rows = self.gateway.order_history("demo", None, limit=1000)
                read_errors = 0
                found = _find_client_order(rows, intent.client_order_id)
                if found is not None:
                    last_order = found
            except (ccxt.NetworkError, ccxt.RequestTimeout) as exc:
                read_errors += 1
                if read_errors >= MAX_READ_ERRORS:
                    return self._stop_outcome(
                        intent,
                        "uncertain",
                        "order_history_unavailable",
                        self._safe_position_state(plan.symbol),
                        last_order,
                        redact_text(exc),
                    )

            if last_order is not None:
                status = str(last_order.get("status") or "").upper()
                executed = _decimal(last_order.get("executedQty"))
                original = _decimal(last_order.get("origQty"))
                if status in TERMINAL_FAILURES:
                    reason = "post_only_canceled" if executed == 0 else "partial_fill_then_canceled"
                    return self._stop_outcome(
                        intent,
                        "stopped" if executed == 0 else "uncertain",
                        reason,
                        self._safe_position_state(plan.symbol),
                        last_order,
                    )
                if status == "FILLED":
                    if executed <= 0 or (original > 0 and executed != original):
                        return self._stop_outcome(
                            intent,
                            "uncertain",
                            "invalid_full_fill_quantities",
                            self._safe_position_state(plan.symbol),
                            last_order,
                        )
                    if str(last_order.get("timeInForce") or "").upper() != "POST_ONLY":
                        return self._stop_outcome(
                            intent,
                            "uncertain",
                            "maker_semantics_not_verified",
                            self._safe_position_state(plan.symbol),
                            last_order,
                        )
                    position = self._safe_position_state(plan.symbol)
                    if self._filled_position_matches(action, position, executed):
                        quote = _decimal(last_order.get("cumQuote"))
                        if quote <= 0:
                            quote = executed * _decimal(last_order.get("avgPrice") or last_order.get("price"))
                        return {
                            "status": "FILLED",
                            "batch_status": "running",
                            "reason": "filled_and_position_verified",
                            "position": position,
                            "order_id": str(last_order.get("orderId") or ""),
                            "client_order_id": intent.client_order_id,
                            "action": action,
                            "price": decimal_text(_decimal(last_order.get("avgPrice") or last_order.get("price"))),
                            "quantity": decimal_text(executed),
                            "quote_volume": decimal_text(quote),
                            "maker": True,
                        }

            now = self.clock()
            if now >= deadline:
                partial = last_order is not None and _decimal(last_order.get("executedQty")) > 0
                reason = "partial_fill_timeout" if partial else "fill_timeout"
                return self._stop_outcome(
                    intent,
                    "uncertain",
                    reason,
                    self._safe_position_state(plan.symbol),
                    last_order,
                )
            self.sleep(min(plan.poll_interval_seconds, deadline - now))

    def _position_state(self, symbol: str) -> dict[str, Any]:
        rows = self.gateway.positions("demo", symbol)
        active = [row for row in rows if _decimal(summarize_position_size(row)) > 0]
        return {
            "active": bool(active),
            "count": len(active),
            "side": str(active[0].get("side") or "").upper() if len(active) == 1 else None,
            "size": summarize_position_size(active[0]) if len(active) == 1 else "0",
        }

    def _safe_position_state(self, symbol: str) -> dict[str, Any]:
        try:
            return self._position_state(symbol)
        except Exception as exc:  # noqa: BLE001 - state is explicitly marked unknown
            return {"active": None, "count": None, "side": None, "size": None, "error": redact_text(exc)}

    @staticmethod
    def _pre_submit_state_error(action: str, state: dict[str, Any], open_quantity: Decimal | None) -> str | None:
        if action == "open":
            return "position_not_flat_before_open" if state["active"] else None
        if not state["active"] or state["count"] != 1 or state["side"] != "LONG":
            return "expected_long_position_missing_before_close"
        if open_quantity is None or _decimal(state["size"]) != open_quantity:
            return "position_size_changed_before_close"
        return None

    @staticmethod
    def _filled_position_matches(action: str, position: dict[str, Any], executed: Decimal) -> bool:
        if action == "close":
            return position["active"] is False
        return (
            position["active"] is True
            and position["count"] == 1
            and position["side"] == "LONG"
            and _decimal(position["size"]) == executed
        )

    @staticmethod
    def _stop_outcome(
        intent: OrderIntent,
        batch_status: str,
        reason: str,
        position: dict[str, Any],
        order: dict[str, Any] | None,
        error: str | None = None,
    ) -> dict[str, Any]:
        executed = _decimal(order.get("executedQty")) if order else Decimal("0")
        quote = _decimal(order.get("cumQuote")) if order else Decimal("0")
        return {
            "status": str(order.get("status") or "UNKNOWN").upper() if order else "UNKNOWN",
            "batch_status": batch_status,
            "reason": reason,
            "position": position,
            "order_id": str(order.get("orderId") or "") if order else None,
            "client_order_id": intent.client_order_id,
            "executed_quantity": decimal_text(executed),
            "quote_volume": decimal_text(quote),
            "error": error,
        }

    def _finish(
        self,
        progress: _BatchProgress,
        status: str,
        reason: str,
        position: dict[str, Any],
    ) -> dict[str, Any]:
        plan = progress.plan
        return {
            "status": status,
            "reason": reason,
            "plan": plan.as_dict(),
            "client_prefix": progress.prefix,
            "attempt_count": len(progress.attempts),
            "fill_count": len(progress.fills),
            "total_quote_volume": decimal_text(progress.total_quote),
            "opening_quote_volume": decimal_text(progress.opening_quote),
            "closing_quote_volume": decimal_text(progress.closing_quote),
            "target_met": progress.total_quote >= plan.target_quote,
            "final_position": position,
            "elapsed_seconds": round(self.clock() - progress.started, 3),
            "attempts": progress.attempts,
            "fills": progress.fills,
        }


def _best_maker_price(book: dict[str, Any], side: str) -> Decimal:
    levels = book.get("bids" if side == "buy" else "asks") or []
    if not levels or not isinstance(levels[0], (list, tuple)) or not levels[0]:
        raise ValidationError(f"order book has no {'bids' if side == 'buy' else 'asks'}")
    price = _decimal(levels[0][0])
    if price <= 0:
        raise ValidationError("order book returned an invalid maker price")
    return price


def _find_client_order(rows: Any, client_order_id: str) -> dict[str, Any] | None:
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and str(row.get("clientOrderId") or "") == client_order_id:
            return row
    return None


def _decimal(value: Any) -> Decimal:
    try:
        result = Decimal(str(value or "0"))
    except Exception:  # noqa: BLE001 - exchange payload validation converts invalid values to zero
        return Decimal("0")
    return result if result.is_finite() else Decimal("0")


def _client_prefix(symbol: str) -> str:
    stamp = datetime.now(UTC).strftime("%m%d%H%M%S")
    return f"mv-{base_asset(symbol)[:8].lower()}-{stamp}-{uuid.uuid4().hex[:4]}"
