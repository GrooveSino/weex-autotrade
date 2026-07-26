from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from weex_cli.core.models import decimal_text
from weex_cli.exchange.rest.gateway import WeexGateway
from weex_cli.execution.adaptive import (
    TargetExecutionResult,
)
from weex_cli.execution.dust_position_close import (
    DustCloseResult,
)
from weex_cli.execution.reconciliation import (
    LegFillReport,
)

from ..contracts import CycleLegSpec, PairLegPlan, _PendingFillReconciliation
from .termination import is_hard_terminal

if TYPE_CHECKING:
    from ..plan import BetaVolumePlan


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
    spec: CycleLegSpec,
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
        "liquidity_policy_satisfied": not accounting_required or bool(report and report.verified and report.maker_only),
        "dust_market_close": False,
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


def _dust_close_summary(sequence: int, leg_plan: PairLegPlan, result: DustCloseResult) -> dict[str, Any]:
    report = result.report
    verified = bool(report and report.verified)
    return {
        "sequence": sequence,
        "symbol": leg_plan.symbol,
        "action": "close",
        "side": leg_plan.closing_side,
        "position_side": leg_plan.position_side,
        "status": "completed" if verified else "stopped",
        "reason": "authoritative_dust_fill_verified" if verified else "dust_close_audit_pending",
        "verification_status": report.status if report is not None else "fills_pending",
        "accounting_required": True,
        "accounting_verified": verified,
        "accounting_source": "user_trades" if report is not None else None,
        "maker_only": False,
        "liquidity_policy_satisfied": verified,
        "dust_market_close": True,
        "fill_count": report.fill_count if report is not None else 0,
        "quote_volume": decimal_text(report.quote_volume) if report is not None else "0",
        "executed_quantity": decimal_text(report.executed_quantity if report is not None else result.quantity),
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
        "elapsed_ms": 0,
        "submissions": 1,
        "cancels": 0,
        "executor_observation": None,
    }


def owned_position_quantity(legs: list[dict[str, Any]], symbol: str, position_side: str) -> Decimal:
    owned = Decimal(0)
    for leg in legs:
        if str(leg.get("symbol") or "").upper() != symbol.upper():
            continue
        if str(leg.get("position_side") or "").lower() != position_side.lower():
            continue
        if not bool(leg.get("accounting_verified")):
            continue
        quantity = Decimal(str(leg.get("executed_quantity") or 0))
        owned += quantity if leg.get("action") == "open" else -quantity
    return max(Decimal(0), owned)


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
        status = "stopped" if is_hard_terminal(report.status) else "uncertain"
        reason = report.status
    leg.update(
        {
            "status": status,
            "reason": reason,
            "verification_status": report.status,
            "accounting_verified": verified,
            "accounting_source": "user_trades",
            "maker_only": report.maker_only,
            "liquidity_policy_satisfied": report.maker_only,
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


def _leg_exception_summary(sequence: int, spec: CycleLegSpec, reason: str) -> dict[str, Any]:
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
        "liquidity_policy_satisfied": True,
        "dust_market_close": False,
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


def accounting_summary(legs: list[dict[str, Any]]) -> dict[str, Any]:
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
    liquidity_policy_satisfied = verified and all(
        bool(leg.get("liquidity_policy_satisfied", leg.get("maker_only"))) for leg in legs
    )
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
        "liquidity_policy_satisfied": liquidity_policy_satisfied,
        "executed_quote_volume": decimal_text(quote),
        "commission_by_asset": {asset: decimal_text(value) for asset, value in sorted(commission_by_asset.items())},
        "realized_pnl": decimal_text(realized_pnl),
    }
