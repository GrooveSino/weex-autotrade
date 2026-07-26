"""Pure parsing, pricing, and identifier helpers for demo maker batches."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from weex_cli.core.errors import ValidationError
from weex_cli.core.symbols import base_asset


def best_maker_price(book: dict[str, Any], side: str) -> Decimal:
    levels = book.get("bids" if side == "buy" else "asks") or []
    if not levels or not isinstance(levels[0], (list, tuple)) or not levels[0]:
        raise ValidationError(f"order book has no {'bids' if side == 'buy' else 'asks'}")
    price = decimal(levels[0][0])
    if price <= 0:
        raise ValidationError("order book returned an invalid maker price")
    return price


def find_client_order(rows: Any, client_order_id: str) -> dict[str, Any] | None:
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and str(row.get("clientOrderId") or "") == client_order_id:
            return row
    return None


def decimal(value: Any) -> Decimal:
    try:
        result = Decimal(str(value or "0"))
    except Exception:  # noqa: BLE001 - invalid exchange values are not tradeable
        return Decimal("0")
    return result if result.is_finite() else Decimal("0")


def client_prefix(symbol: str) -> str:
    stamp = datetime.now(UTC).strftime("%m%d%H%M%S")
    return f"mv-{base_asset(symbol)[:8].lower()}-{stamp}-{uuid.uuid4().hex[:4]}"
