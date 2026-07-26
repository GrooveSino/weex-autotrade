"""Read-only pricing, account boundary, and result helpers for live volume."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import ROUND_UP, Decimal
from typing import Any

from weex_cli.core.errors import ValidationError
from weex_cli.core.models import decimal_text
from weex_cli.exchange.rest.gateway import WeexGateway, summarize_position_size
from weex_cli.execution.adaptive import TargetExecutionResult
from weex_cli.execution.venues import LiveAdaptiveMakerVenue


def quantity_for_turnover(gateway: WeexGateway, symbol: str, turnover: Decimal, price: Decimal) -> Decimal:
    step = gateway.amount_step(symbol)
    raw = turnover / (Decimal(2) * price)
    lower = gateway.amount_to_precision(symbol, raw)
    upper_raw = (raw / step).to_integral_value(rounding=ROUND_UP) * step
    upper = gateway.amount_to_precision(symbol, upper_raw)
    candidates = [quantity for quantity in {lower, upper, step} if quantity > 0]
    if not candidates:
        raise ValidationError("round turnover is below the market minimum quantity")
    return min(candidates, key=lambda quantity: (abs(Decimal(2) * quantity * price - turnover), quantity))


def mid_price(gateway: WeexGateway, symbol: str) -> Decimal:
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


def available_quote(gateway: WeexGateway) -> Decimal:
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


def active_positions(gateway: WeexGateway, symbol: str) -> list[Mapping[str, Any]]:
    return [
        row
        for row in gateway.positions("live", symbol)
        if isinstance(row, Mapping) and Decimal(summarize_position_size(row)) > 0
    ]


def submitted_order_ids(result: TargetExecutionResult) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            str(event.get("order_id"))
            for event in result.events
            if event.get("event") == "submit" and event.get("order_id")
        )
    )


def safe_position(venue: LiveAdaptiveMakerVenue) -> float | None:
    try:
        return venue.position_quantity()
    except Exception:  # noqa: BLE001 - uncertainty is represented explicitly
        return None


def is_flat(venue: LiveAdaptiveMakerVenue, amount_step: Decimal) -> bool:
    position = safe_position(venue)
    return position is not None and abs(Decimal(str(position))) <= amount_step / 2


def row_count(payload: Any) -> int:
    if isinstance(payload, list):
        return len(payload)
    if isinstance(payload, Mapping):
        rows = payload.get("rows") or payload.get("data") or payload.get("list")
        return len(rows) if isinstance(rows, list) else 0
    return 0


def leg_error(action: str, attempt: int, reason: str, *, uncertain: bool) -> dict[str, Any]:
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


def round_outcome(
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
