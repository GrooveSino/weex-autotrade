"""Read-only position and terminal-order verification for demo batches."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from weex_cli.core.models import OrderIntent, decimal_text
from weex_cli.core.redaction import redact_text
from weex_cli.exchange.rest.gateway import WeexGateway, summarize_position_size

from .support import decimal


def position_state(gateway: WeexGateway, symbol: str) -> dict[str, Any]:
    rows = gateway.positions("demo", symbol)
    active = [row for row in rows if decimal(summarize_position_size(row)) > 0]
    return {
        "active": bool(active),
        "count": len(active),
        "side": str(active[0].get("side") or "").upper() if len(active) == 1 else None,
        "size": summarize_position_size(active[0]) if len(active) == 1 else "0",
    }


def safe_position_state(gateway: WeexGateway, symbol: str) -> dict[str, Any]:
    try:
        return position_state(gateway, symbol)
    except Exception as exc:  # noqa: BLE001 - state is explicitly marked unknown
        return {"active": None, "count": None, "side": None, "size": None, "error": redact_text(exc)}


def pre_submit_state_error(action: str, state: dict[str, Any], open_quantity: Decimal | None) -> str | None:
    if action == "open":
        return "position_not_flat_before_open" if state["active"] else None
    if not state["active"] or state["count"] != 1 or state["side"] != "LONG":
        return "expected_long_position_missing_before_close"
    if open_quantity is None or decimal(state["size"]) != open_quantity:
        return "position_size_changed_before_close"
    return None


def filled_position_matches(action: str, position: dict[str, Any], executed: Decimal) -> bool:
    if action == "close":
        return position["active"] is False
    return (
        position["active"] is True
        and position["count"] == 1
        and position["side"] == "LONG"
        and decimal(position["size"]) == executed
    )


def stop_outcome(
    intent: OrderIntent,
    batch_status: str,
    reason: str,
    position: dict[str, Any],
    order: dict[str, Any] | None,
    error: str | None = None,
) -> dict[str, Any]:
    executed = decimal(order.get("executedQty")) if order else Decimal("0")
    quote = decimal(order.get("cumQuote")) if order else Decimal("0")
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
