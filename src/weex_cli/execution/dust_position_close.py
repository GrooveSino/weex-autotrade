from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

from weex_cli.core.errors import ValidationError
from weex_cli.core.models import decimal_text
from weex_cli.core.reliability import NETWORK_ERRORS
from weex_cli.exchange.rest.gateway import summarize_position_size
from weex_cli.execution.reconciliation import LegFillReconciler, LegFillReport, LegFillRequest

ELIGIBLE_FALLBACK_REASONS = {
    "amount_precision_rejected",
    "below_minimum",
    "maker_completed_with_residual",
    "minimum_quantity_rejected",
}
MINIMUM_REJECTION_REASONS = {"amount_precision_rejected", "minimum_quantity_rejected"}


class MarketCloseStore(Protocol):
    def claim_market_close_intent(self, plan: Any, key: str, *, created_at_ms: int) -> bool: ...


@dataclass(frozen=True, slots=True)
class DustPosition:
    position_id: str
    quantity: Decimal
    quote: Decimal


@dataclass(frozen=True, slots=True)
class DustCloseResult:
    attempted: bool
    flat: bool
    uncertain: bool
    reason: str
    report: LegFillReport | None = None
    quantity: Decimal = Decimal(0)
    quote: Decimal = Decimal(0)


def close_dust_position_once(
    *,
    gateway: Any,
    store: MarketCloseStore,
    plan: Any,
    cycle: int,
    symbol: str,
    position_side: str,
    owned_quantity: Decimal,
    amount_step: Decimal,
    maker_reason: str | None,
    reconciler: LegFillReconciler,
    now_ms: Callable[[], int],
    sleep: Callable[[float], None],
    emit: Callable[..., None],
) -> DustCloseResult:
    if maker_reason not in ELIGIBLE_FALLBACK_REASONS:
        return DustCloseResult(False, False, False, "maker_failure_not_dust_eligible")
    try:
        position = _position(gateway, symbol, position_side)
        minimum = _minimum_amount(gateway, symbol)
    except Exception as exc:  # noqa: BLE001 - eligibility must be fully observable
        return DustCloseResult(False, False, False, f"dust_eligibility_unavailable:{type(exc).__name__.lower()}")
    if position is None or position.quantity <= amount_step / 2:
        return DustCloseResult(False, True, False, "already_flat")
    if owned_quantity <= 0 or position.quantity > owned_quantity + amount_step / 2:
        return DustCloseResult(False, False, False, "position_not_owned_by_execution")
    minimum_rejected = maker_reason in MINIMUM_REJECTION_REASONS
    below_minimum = minimum is not None and position.quantity < minimum
    below_quote_limit = position.quote > 0 and position.quote <= plan.dust_close_max_quote
    threshold_eligible = maker_reason == "maker_completed_with_residual" and below_quote_limit
    if not (minimum_rejected or below_minimum or threshold_eligible):
        return DustCloseResult(False, False, False, "position_not_dust")
    if gateway.open_orders(symbol, mode="live") or _row_count(gateway.algo_orders(symbol)):
        return DustCloseResult(False, False, True, "active_order_blocks_dust_close")

    reason = "minimum_rejected" if minimum_rejected else "below_minimum" if below_minimum else "quote_threshold"
    emit(
        "dust_close_detected",
        round=cycle,
        symbol=symbol,
        action="close",
        side=position_side,
        quantity=decimal_text(position.quantity),
        quote=decimal_text(position.quote),
        reason=reason,
    )
    key = f"{plan.plan_id}:{cycle}:{symbol}:{position_side}"
    intent_at_ms = now_ms()
    if not store.claim_market_close_intent(plan, key, created_at_ms=intent_at_ms):
        emit(
            "market_close_uncertain",
            round=cycle,
            symbol=symbol,
            action="close",
            side=position_side,
            reason="intent_already_exists",
        )
        return DustCloseResult(
            False,
            False,
            True,
            "market_close_already_attempted",
            quantity=position.quantity,
            quote=position.quote,
        )
    emit(
        "market_close_intent_persisted",
        round=cycle,
        symbol=symbol,
        action="close",
        side=position_side,
        reason=reason,
    )

    response: Any = None
    mutation_uncertain = False
    try:
        response = gateway.close_position_id(symbol, position.position_id)
    except NETWORK_ERRORS:
        mutation_uncertain = True
    except Exception as exc:  # noqa: BLE001 - explicit venue rejection is terminal and never retried
        emit(
            "market_close_uncertain",
            round=cycle,
            symbol=symbol,
            action="close",
            side=position_side,
            reason=f"market_close_rejected:{type(exc).__name__.lower()}",
        )
        return DustCloseResult(
            True,
            False,
            True,
            f"market_close_rejected:{type(exc).__name__.lower()}",
            quantity=position.quantity,
            quote=position.quote,
        )
    if _response_rejected(response):
        emit(
            "market_close_uncertain",
            round=cycle,
            symbol=symbol,
            action="close",
            side=position_side,
            reason="market_close_rejected",
        )
        return DustCloseResult(
            True,
            False,
            True,
            "market_close_rejected",
            quantity=position.quantity,
            quote=position.quote,
        )
    if not mutation_uncertain and not _response_accepted(response):
        mutation_uncertain = True
    order_id = _accepted_order_id(response)
    if not mutation_uncertain:
        emit(
            "market_close_accepted",
            round=cycle,
            symbol=symbol,
            action="close",
            side=position_side,
            reason=reason,
        )
    flat = _verify_flat(gateway, symbol, position_side, amount_step, sleep)
    if not flat:
        event_reason = "market_close_transport_uncertain" if mutation_uncertain else "market_close_not_flat"
        emit(
            "market_close_uncertain",
            round=cycle,
            symbol=symbol,
            action="close",
            side=position_side,
            reason=event_reason,
        )
        return DustCloseResult(
            True,
            False,
            True,
            event_reason,
            quantity=position.quantity,
            quote=position.quote,
        )

    try:
        report = _reconcile(reconciler, symbol, position.quantity, amount_step, order_id, intent_at_ms, now_ms())
    except Exception:  # noqa: BLE001 - a flat position is operationally safe while audit catches up
        report = None
    emit(
        "market_close_verified",
        round=cycle,
        symbol=symbol,
        action="close",
        side=position_side,
        fill_count=report.fill_count if report is not None else 0,
        quote_volume=decimal_text(report.quote_volume) if report is not None else "0",
        verified=bool(report and report.verified),
    )
    return DustCloseResult(
        True,
        True,
        False,
        "verified" if report and report.verified else "audit_pending",
        report,
        position.quantity,
        position.quote,
    )


def _position(gateway: Any, symbol: str, side: str) -> DustPosition | None:
    matches: list[DustPosition] = []
    for row in gateway.positions("live", symbol):
        info = row.get("info") if isinstance(row.get("info"), Mapping) else {}
        observed_side = str(row.get("side") or info.get("positionSide") or info.get("side") or "").lower()
        quantity = abs(Decimal(summarize_position_size(row)))
        if observed_side != side or quantity <= 0:
            continue
        position_id = str(row.get("id") or info.get("positionId") or info.get("id") or "").strip()
        if not position_id:
            raise ValueError("position id unavailable")
        matches.append(DustPosition(position_id, quantity, _quote_value(gateway, symbol, row, info, quantity)))
    if len(matches) > 1:
        raise ValueError("multiple owned positions")
    return matches[0] if matches else None


def _quote_value(
    gateway: Any,
    symbol: str,
    row: Mapping[str, Any],
    info: Mapping[str, Any],
    quantity: Decimal,
) -> Decimal:
    for key in ("notional", "openValue", "positionValue"):
        raw = row.get(key) if row.get(key) is not None else info.get(key)
        try:
            value = abs(Decimal(str(raw)))
        except Exception:  # noqa: BLE001
            continue
        if value.is_finite() and value > 0:
            return value
    book = gateway.order_book(symbol, 5)
    bids, asks = book.get("bids") or [], book.get("asks") or []
    if not bids or not asks:
        raise ValueError("market unavailable")
    return quantity * (Decimal(str(bids[0][0])) + Decimal(str(asks[0][0]))) / 2


def _minimum_amount(gateway: Any, symbol: str) -> Decimal | None:
    reader = getattr(gateway, "minimum_amount", None)
    return reader(symbol) if callable(reader) else None


def _verify_flat(gateway: Any, symbol: str, side: str, step: Decimal, sleep: Callable[[float], None]) -> bool:
    for attempt in range(4):
        try:
            observed = _position(gateway, symbol, side)
        except NETWORK_ERRORS:
            if attempt < 3:
                sleep(0.25 * (2**attempt))
            continue
        if observed is None or observed.quantity <= step / 2:
            return True
        if attempt < 3:
            sleep(0.25 * (2**attempt))
    return False


def classify_minimum_order_rejection(exc: Exception) -> str | None:
    """Map only explicit quantity-rule failures to the dust-close eligibility codes."""
    message = str(exc).lower()
    if isinstance(exc, ValidationError):
        if "quantity is below weex precision" in message:
            return "amount_precision_rejected"
        if "quantity" in message and ("minimum" in message or "min order" in message):
            return "minimum_quantity_rejected"
    if type(exc).__name__ not in {"BadRequest", "InvalidOrder"}:
        return None
    if "precision" in message and ("amount" in message or "quantity" in message or "size" in message):
        return "amount_precision_rejected"
    minimum_markers = ("min amount", "minimum amount", "min order", "minimum order", "size too small")
    return "minimum_quantity_rejected" if any(marker in message for marker in minimum_markers) else None


def _response_rejected(response: Any) -> bool:
    rows = response if isinstance(response, list) else [response]
    mappings = [row for row in rows if isinstance(row, Mapping)]
    return bool(mappings) and all(row.get("success") is False for row in mappings)


def _response_accepted(response: Any) -> bool:
    rows = response if isinstance(response, list) else [response]
    return any(isinstance(row, Mapping) and row.get("success") is True for row in rows)


def _accepted_order_id(response: Any) -> str | None:
    rows = response if isinstance(response, list) else [response]
    for row in rows:
        if not isinstance(row, Mapping) or row.get("success") is False:
            continue
        value = row.get("successOrderId") or row.get("orderId")
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _reconcile(
    reconciler: LegFillReconciler,
    symbol: str,
    quantity: Decimal,
    step: Decimal,
    order_id: str | None,
    started_at_ms: int,
    ended_at_ms: int,
) -> LegFillReport | None:
    if not order_id:
        return None
    report = reconciler.reconcile(
        LegFillRequest(
            sequence=0,
            symbol=symbol,
            action="close",
            expected_quantity=quantity,
            tolerance_quantity=step / 2,
            order_ids=(order_id,),
            started_at_ms=started_at_ms,
            ended_at_ms=ended_at_ms,
            maker_only_required=False,
        )
    )
    return report if report.verified else None


def _row_count(rows: Any) -> int:
    if isinstance(rows, list):
        return len(rows)
    if isinstance(rows, Mapping):
        values = rows.get("data") or rows.get("list") or rows.get("rows")
        return len(values) if isinstance(values, list) else int(bool(rows))
    return 0
