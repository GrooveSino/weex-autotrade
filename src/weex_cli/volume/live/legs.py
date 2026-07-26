"""One-leg execution and authoritative fill-accounting behavior."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from weex_cli.core.models import decimal_text
from weex_cli.execution.adaptive import TargetRequest
from weex_cli.execution.adaptive_maker import AdaptiveMakerPolicy
from weex_cli.execution.adaptive_volume import REAL_POLICY
from weex_cli.execution.reconciliation import LegFillReport, LegFillRequest

from .support import leg_error, safe_position, submitted_order_ids


class LiveMakerVolumeLegsMixin:
    def _execute_leg(
        self,
        *,
        round_number: int,
        attempt: int,
        action: str,
        side: str,
        target_position: float,
        venue: Any,
        client_prefix: str,
    ) -> dict[str, Any]:
        assert self.plan is not None
        started_at_ms = self.now_ms()
        start_position = safe_position(venue)
        if start_position is None:
            return leg_error(action, attempt, "starting_position_unavailable", uncertain=True)
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
            return leg_error(action, attempt, reason, uncertain=True)

        end_position = Decimal(str(result.final_position))
        executed_quantity = abs(end_position - Decimal(str(result.start_position)))
        report: LegFillReport | None = None
        reconciliation_error: str | None = None
        order_ids = submitted_order_ids(result)
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
