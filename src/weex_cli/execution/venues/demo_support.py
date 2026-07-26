"""Pure Demo Maker venue normalization helpers."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from weex_cli.execution.adaptive import VenueOrder
from weex_cli.execution.adaptive_maker import Side


def not_submitted(side: Side, client_order_id: str, quantity: float, price: float, reason: str) -> VenueOrder:
    return VenueOrder(
        order_id="",
        client_order_id=client_order_id,
        side=side,
        price=price,
        quantity=quantity,
        filled_quantity=0.0,
        cumulative_quote=0.0,
        status="not_submitted",
        post_only=True,
        maker=None,
        cancellation_reason=reason,
    )


def levels(value: Any) -> list[tuple[float, float]]:
    rows: list[tuple[float, float]] = []
    for row in value if isinstance(value, list) else []:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        price, quantity = float(row[0]), float(row[1])
        if math.isfinite(price) and math.isfinite(quantity) and price > 0 and quantity >= 0:
            rows.append((price, quantity))
    return rows


def tick_size(bids: list[tuple[float, float]], asks: list[tuple[float, float]], bid: float, ask: float) -> float:
    prices = sorted({price for price, _ in [*bids, *asks]})
    differences = [right - left for left, right in zip(prices, prices[1:], strict=False) if right > left]
    return min(differences) if differences else ask - bid


def same_order(row: Mapping[str, Any], order_id: str, client_order_id: str) -> bool:
    return (
        str(row.get("id") or row.get("orderId") or "") == order_id
        or str(row.get("clientOrderId") or "") == client_order_id
    )


def venue_order(row: Mapping[str, Any]) -> VenueOrder:
    status_text = str(row.get("status") or "").upper()
    status = {
        "OPEN": "new",
        "PENDING": "new",
        "CANCELING": "new",
        "PARTIALLY_FILLED": "partially_filled",
        "FILLED": "filled",
        "CANCELED": "canceled",
        "CANCELLED": "canceled",
        "REJECTED": "rejected",
        "EXPIRED": "rejected",
    }.get(status_text, "unknown")
    side = str(row.get("orderSide") or row.get("side") or "").lower()
    normalized_side: Side = "sell" if side == "sell" else "buy"
    price = float(row.get("price") or row.get("avgPrice") or 0)
    quantity = float(row.get("size") or row.get("origQty") or 0)
    filled = float(row.get("cumFillSize") or row.get("executedQty") or 0)
    quote = float(row.get("cumFillValue") or row.get("cumQuote") or 0)
    post_only = str(row.get("timeInForce") or "").upper() == "POST_ONLY"
    maker = True if filled > 0 and post_only else None
    return VenueOrder(
        order_id=str(row.get("id") or row.get("orderId") or ""),
        client_order_id=str(row.get("clientOrderId") or ""),
        side=normalized_side,
        price=price,
        quantity=quantity,
        filled_quantity=filled,
        cumulative_quote=quote,
        status=status,  # type: ignore[arg-type]
        post_only=post_only,
        maker=maker,
        cancellation_reason=str(row.get("cancelReason") or "").upper() or None,
    )


def history_order(row: Mapping[str, Any]) -> VenueOrder:
    order = venue_order(row)
    if order.status == "canceled" and not order.cancellation_reason:
        return unknown_order(order, cancellation_reason="CANCELED_REASON_UNKNOWN")
    return order


def unknown_order(order: VenueOrder, *, cancellation_reason: str | None = None) -> VenueOrder:
    return VenueOrder(
        order.order_id,
        order.client_order_id,
        order.side,
        order.price,
        order.quantity,
        order.filled_quantity,
        order.cumulative_quote,
        "unknown",
        order.post_only,
        order.maker,
        order.queue_ahead,
        cancellation_reason or order.cancellation_reason,
    )
