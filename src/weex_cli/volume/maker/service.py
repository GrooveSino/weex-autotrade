"""Sequential demo Maker execution with one-shot submission safety."""

from __future__ import annotations

import time
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import ccxt

from weex_cli.core.errors import SubmissionUncertainError, ValidationError
from weex_cli.core.models import OrderIntent, decimal_text
from weex_cli.core.redaction import redact_text
from weex_cli.exchange.rest.gateway import WeexGateway
from weex_cli.execution.service import TradingService

from .contracts import VOLUME_BUFFER, MakerVolumePlan
from .progress import BatchProgress, finish
from .state import filled_position_matches, position_state, pre_submit_state_error, safe_position_state, stop_outcome
from .support import best_maker_price, client_prefix, decimal, find_client_order

MAX_READ_ERRORS = 3
MIN_SUBMIT_INTERVAL = 10.1
TERMINAL_FAILURES = {"CANCELED", "CANCELLED", "EXPIRED", "REJECTED"}


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
        self.prefix_factory = prefix_factory or client_prefix

    def run(self, plan: MakerVolumePlan) -> dict[str, Any]:
        progress = BatchProgress(plan=plan, prefix=self.prefix_factory(plan.symbol), started=self.clock())
        initial_state = position_state(self.gateway, plan.symbol)
        if initial_state["active"]:
            return self._finish(progress, "stopped", "starting_position_not_flat", initial_state)

        for sequence in range(1, plan.fills + 1):
            stopped = self._execute_sequence(progress, sequence)
            if stopped is not None:
                return stopped

        final_state = position_state(self.gateway, plan.symbol)
        completed = progress.total_quote >= plan.target_quote and not final_state["active"]
        status = "completed" if completed else "stopped"
        reason = "target_reached" if completed else "target_not_reached_or_position_not_flat"
        return self._finish(progress, status, reason, final_state)

    def _execute_sequence(self, progress: BatchProgress, sequence: int) -> dict[str, Any] | None:
        plan = progress.plan
        action = "open" if sequence % 2 else "close"
        self._throttle(progress.last_submit_at)
        state = position_state(self.gateway, plan.symbol)
        state_error = pre_submit_state_error(action, state, progress.open_quantity)
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

    def _prepare_order(self, progress: BatchProgress, sequence: int, action: str) -> tuple[OrderIntent, dict[str, Any]]:
        plan = progress.plan
        side = "buy" if action == "open" else "sell"
        price = best_maker_price(self.gateway.order_book(plan.symbol, 1), side)
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
        return intent, {
            "sequence": sequence,
            "action": action,
            "client_order_id": client_order_id,
            "side": side.upper(),
            "price": decimal_text(price),
            "quantity": decimal_text(quantity),
            "planned_quote": decimal_text(quantity * price),
            "status": "submitting",
        }

    def _submit_once(
        self, progress: BatchProgress, intent: OrderIntent, attempt: dict[str, Any]
    ) -> dict[str, Any] | None:
        try:
            submission = self.trading.submit_order(intent, allow_existing=True)
        except SubmissionUncertainError as exc:
            attempt.update(status="uncertain", error=redact_text(exc))
            return self._finish(
                progress,
                "uncertain",
                "submission_outcome_uncertain",
                safe_position_state(self.gateway, progress.plan.symbol),
            )
        except ccxt.BaseError as exc:
            attempt.update(status="rejected", error=redact_text(exc))
            return self._finish(
                progress,
                "stopped",
                "submission_rejected",
                safe_position_state(self.gateway, progress.plan.symbol),
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
            safe_position_state(self.gateway, progress.plan.symbol),
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
                found = find_client_order(rows, intent.client_order_id)
                if found is not None:
                    last_order = found
            except (ccxt.NetworkError, ccxt.RequestTimeout) as exc:
                read_errors += 1
                if read_errors >= MAX_READ_ERRORS:
                    return stop_outcome(
                        intent,
                        "uncertain",
                        "order_history_unavailable",
                        safe_position_state(self.gateway, plan.symbol),
                        last_order,
                        redact_text(exc),
                    )

            outcome = self._terminal_outcome(plan, intent, action, last_order)
            if outcome is not None:
                return outcome
            now = self.clock()
            if now >= deadline:
                partial = last_order is not None and decimal(last_order.get("executedQty")) > 0
                reason = "partial_fill_timeout" if partial else "fill_timeout"
                return stop_outcome(
                    intent,
                    "uncertain",
                    reason,
                    safe_position_state(self.gateway, plan.symbol),
                    last_order,
                )
            self.sleep(min(plan.poll_interval_seconds, deadline - now))

    def _terminal_outcome(
        self,
        plan: MakerVolumePlan,
        intent: OrderIntent,
        action: str,
        order: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if order is None:
            return None
        status = str(order.get("status") or "").upper()
        executed = decimal(order.get("executedQty"))
        original = decimal(order.get("origQty"))
        if status in TERMINAL_FAILURES:
            reason = "post_only_canceled" if executed == 0 else "partial_fill_then_canceled"
            return stop_outcome(
                intent,
                "stopped" if executed == 0 else "uncertain",
                reason,
                safe_position_state(self.gateway, plan.symbol),
                order,
            )
        if status != "FILLED":
            return None
        if executed <= 0 or (original > 0 and executed != original):
            return stop_outcome(
                intent,
                "uncertain",
                "invalid_full_fill_quantities",
                safe_position_state(self.gateway, plan.symbol),
                order,
            )
        if str(order.get("timeInForce") or "").upper() != "POST_ONLY":
            return stop_outcome(
                intent,
                "uncertain",
                "maker_semantics_not_verified",
                safe_position_state(self.gateway, plan.symbol),
                order,
            )
        position = safe_position_state(self.gateway, plan.symbol)
        if not filled_position_matches(action, position, executed):
            return None
        quote = decimal(order.get("cumQuote"))
        if quote <= 0:
            quote = executed * decimal(order.get("avgPrice") or order.get("price"))
        return {
            "status": "FILLED",
            "batch_status": "running",
            "reason": "filled_and_position_verified",
            "position": position,
            "order_id": str(order.get("orderId") or ""),
            "client_order_id": intent.client_order_id,
            "action": action,
            "price": decimal_text(decimal(order.get("avgPrice") or order.get("price"))),
            "quantity": decimal_text(executed),
            "quote_volume": decimal_text(quote),
            "maker": True,
        }

    def _finish(
        self,
        progress: BatchProgress,
        status: str,
        reason: str,
        position: dict[str, Any],
    ) -> dict[str, Any]:
        return finish(progress, status, reason, position, now=self.clock())
