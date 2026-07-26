"""Pure exchange-row normalization helpers for the live Maker venue."""

from __future__ import annotations

import math
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from weex_cli.execution.adaptive import VenueOrder
from weex_cli.execution.adaptive_maker import Side


def _not_submitted(side: Side, client_order_id: str, quantity: Decimal, price: Decimal, reason: str) -> VenueOrder:
    return VenueOrder(
        order_id="",
        client_order_id=client_order_id,
        side=side,
        price=float(price),
        quantity=float(quantity),
        filled_quantity=0.0,
        cumulative_quote=0.0,
        status="not_submitted",
        post_only=True,
        maker=None,
        cancellation_reason=reason,
    )


def _same_order(row: Mapping[str, Any], order_id: str, client_order_id: str) -> bool:
    info = row.get("info") if isinstance(row.get("info"), Mapping) else {}
    return (
        str(row.get("id") or info.get("orderId") or "") == order_id
        or str(row.get("clientOrderId") or info.get("clientOrderId") or info.get("newClientOrderId") or "")
        == client_order_id
    )


def _venue_order(
    row: Mapping[str, Any],
    *,
    fallback_side: Side,
    fallback_client_id: str,
    fallback_quantity: float = 0,
    fallback_price: float = 0,
) -> VenueOrder:
    info = row.get("info") if isinstance(row.get("info"), Mapping) else {}
    status_text = str(row.get("status") or info.get("status") or "").lower()
    status = {
        "open": "new",
        "new": "new",
        "pending": "new",
        "partially_filled": "partially_filled",
        "partial": "partially_filled",
        "closed": "filled",
        "filled": "filled",
        "canceled": "canceled",
        "cancelled": "canceled",
        "rejected": "rejected",
        "expired": "rejected",
    }.get(status_text, "unknown")
    side_text = str(row.get("side") or info.get("side") or fallback_side).lower()
    side: Side = "sell" if side_text == "sell" else "buy"
    quantity = _number(row.get("amount", info.get("origQty", info.get("size", fallback_quantity))))
    filled = _number(row.get("filled", info.get("executedQty", info.get("cumFillSize", 0))))
    price = _number(row.get("price", info.get("price", fallback_price)))
    if quantity <= 0:
        quantity = fallback_quantity
    if price <= 0:
        price = fallback_price
    average = _number(row.get("average", info.get("avgPrice", 0)))
    cost = _number(row.get("cost", info.get("cumQuote", info.get("cumFillValue", 0))))
    if cost <= 0 and filled > 0:
        cost = filled * (average or price)
    time_in_force = str(row.get("timeInForce") or info.get("timeInForce") or "").upper()
    post_only = row.get("postOnly") is True or time_in_force == "POST_ONLY"
    maker = True if filled > 0 and post_only else None
    reason = str(info.get("cancelReason") or row.get("cancelReason") or "").upper() or None
    return VenueOrder(
        order_id=str(row.get("id") or info.get("orderId") or ""),
        client_order_id=str(
            row.get("clientOrderId") or info.get("clientOrderId") or info.get("newClientOrderId") or fallback_client_id
        ),
        side=side,
        price=price,
        quantity=quantity,
        filled_quantity=filled,
        cumulative_quote=cost,
        status=status,  # type: ignore[arg-type]
        post_only=post_only,
        maker=maker,
        cancellation_reason=reason,
    )


def _number(value: Any) -> float:
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0


def _row_count(value: Any) -> int:
    if isinstance(value, list):
        return len(value)
    if isinstance(value, Mapping):
        rows = value.get("rows") or value.get("data") or value.get("list") or []
        return len(rows) if isinstance(rows, list) else 0
    return 0


def _levels(value: Any) -> list[tuple[float, float]]:
    rows: list[tuple[float, float]] = []
    for row in value if isinstance(value, list) else []:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        price, quantity = _number(row[0]), _number(row[1])
        if price > 0 and quantity >= 0:
            rows.append((price, quantity))
    return rows


def _tick_size(bids: list[tuple[float, float]], asks: list[tuple[float, float]], bid: float, ask: float) -> float:
    prices = sorted({price for price, _ in [*bids, *asks]})
    differences = [right - left for left, right in zip(prices, prices[1:], strict=False) if right > left]
    return min(differences) if differences else ask - bid
