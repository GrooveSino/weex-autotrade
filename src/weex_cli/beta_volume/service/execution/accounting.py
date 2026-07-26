from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from weex_cli.core.models import decimal_text

from ...accounting.fills import (
    _apply_fill_report,
    accounting_summary,
)
from ...accounting.payload import _result_payload
from ...accounting.termination import is_hard_terminal
from ...contracts import (
    POST_FLAT_ACCOUNTING_ATTEMPTS,
    RETRYABLE_ACCOUNTING_STATUSES,
    ExecutionLane,
    _PendingFillReconciliation,
)
from ...plan import BetaVolumePlan
from ...safety import (
    _available_quote,
    _ensure_lane_leverage,
    _row_count,
    select_leverage,
)


class ExecutionAccountingMixin:
    def _refresh_pending_accounting(
        self,
        round_number: int,
        legs: list[dict[str, Any]],
        lanes: Mapping[str, ExecutionLane],
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
                    "stopped" if is_hard_terminal(reason) else "accounting_uncertain",
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

    def _final_acceptance(
        self,
        plan: BetaVolumePlan,
        summaries: list[dict[str, Any]],
        cycles: list[dict[str, Any]],
        total_quote: Decimal,
        lanes: Mapping[str, ExecutionLane],
        preflight: Mapping[str, Any],
        execution_started_ms: int,
        *,
        minimum_accepted_quote: Decimal | None = None,
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
        accounting = accounting_summary(summaries)
        required_quote = plan.target_turnover_quote if minimum_accepted_quote is None else minimum_accepted_quote
        completed = (
            total_quote >= required_quote
            and flat
            and no_orders
            and accounting["verified"]
            and accounting["liquidity_policy_satisfied"]
        )
        self._emit(
            "final_acceptance_completed",
            completed=completed,
            flat=flat,
            no_orders=no_orders,
            accounting_verified=accounting["verified"],
            maker_only=accounting["maker_only"],
            liquidity_policy_satisfied=accounting["liquidity_policy_satisfied"],
        )
        return self._finish(
            plan,
            "completed" if completed else "uncertain",
            (
                "paired_target_completed_with_tolerance"
                if completed and total_quote < plan.target_turnover_quote
                else "paired_target_completed"
            )
            if completed
            else "final_acceptance_invariant_failed",
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
        lanes: Mapping[str, ExecutionLane],
        preflight: Mapping[str, Any],
        execution_started_ms: int,
    ) -> dict[str, Any]:
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
        self._emit(
            "workflow_finished",
            status=status,
            reason=reason,
            executed_quote_volume=decimal_text(total_quote),
        )
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
