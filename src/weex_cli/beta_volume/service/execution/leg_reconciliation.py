from __future__ import annotations

from decimal import Decimal
from typing import Any

from weex_cli.core.models import decimal_text
from weex_cli.execution.adaptive import (
    TargetExecutionResult,
)
from weex_cli.execution.reconciliation import (
    LegFillReport,
    LegFillRequest,
)
from weex_cli.execution.venues import LiveAdaptiveMakerVenue

from ...accounting.fills import (
    _history_order_ids,
    _leg_summary,
    _submitted_order_ids,
)
from ...accounting.termination import is_hard_terminal
from ...contracts import (
    RETRYABLE_ACCOUNTING_STATUSES,
    CycleLegSpec,
    ExecutionLane,
    _PendingFillReconciliation,
)
from ...plan import BetaVolumePlan
from ...safety import (
    _row_count,
)


class LegReconciliationMixin:
    def _reconcile_leg_result(
        self,
        plan: BetaVolumePlan,
        sequence: int,
        spec: CycleLegSpec,
        lane: ExecutionLane,
        round_number: int,
        venue: LiveAdaptiveMakerVenue,
        start_position: float,
        started_at_ms: int,
        result: TargetExecutionResult,
    ) -> tuple[dict[str, Any], tuple[str, str] | None]:
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
            status = "stopped" if is_hard_terminal(reason) else "accounting_uncertain"
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
            executed_quantity=decimal_text(report.executed_quantity if report is not None else executed_quantity),
            position_side=spec.plan.position_side,
            maker_count=report.maker_count if report is not None else 0,
            taker_count=report.taker_count if report is not None else 0,
            unknown_liquidity_count=report.unknown_liquidity_count if report is not None else 0,
            fill_count=report.fill_count if report is not None else 0,
            elapsed_ms=result.elapsed_ms,
            submissions=result.submissions,
            cancels=result.cancels,
        )
        return summary, None
