"""Shared total keys and deterministic Demo-row normalization."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from .contracts import CachedTrade

DECIMAL_TOTAL_KEYS = (
    "total_quote",
    "opening_quote",
    "closing_quote",
    "unknown_action_quote",
    "maker_quote",
    "taker_quote",
    "unknown_liquidity_quote",
)
COUNT_TOTAL_KEYS = ("trade_count", "maker_count", "taker_count", "unknown_liquidity_count")
TOTAL_KEYS = (*DECIMAL_TOTAL_KEYS, *COUNT_TOTAL_KEYS)


def empty_totals() -> dict[str, Decimal | int]:
    return {**{key: Decimal(0) for key in DECIMAL_TOTAL_KEYS}, **{key: 0 for key in COUNT_TOTAL_KEYS}}


def normalize_demo_rows(rows: list[dict[str, Any]]) -> list[CachedTrade]:
    normalized: list[CachedTrade] = []
    for row in rows:
        quantity = _decimal(row.get("executedQty"))
        if quantity <= 0:
            continue
        quote = _decimal(row.get("cumQuote"))
        if quote <= 0:
            quote = quantity * _decimal(row.get("avgPrice") or row.get("price"))
        timestamp = _integer(row.get("updateTime") or row.get("time"))
        order_id = str(row.get("orderId") or row.get("id") or "")
        if quote <= 0 or timestamp is None or not order_id:
            continue
        normalized.append(
            CachedTrade(
                trade_id=order_id,
                order_id=order_id,
                symbol=str(row.get("symbol") or "UNKNOWN").upper(),
                timestamp=timestamp,
                quote_volume=quote,
                action=_position_action(row),
                liquidity="maker" if str(row.get("timeInForce") or "").upper() == "POST_ONLY" else "unknown_liquidity",
            )
        )
    return normalized


def _position_action(row: dict[str, Any]) -> str:
    side = str(row.get("side") or "").upper()
    position_side = str(row.get("positionSide") or "").upper()
    if (side, position_side) in {("BUY", "LONG"), ("SELL", "SHORT")}:
        return "opening"
    if (side, position_side) in {("SELL", "LONG"), ("BUY", "SHORT")}:
        return "closing"
    return "unknown_action"


def _decimal(value: object) -> Decimal:
    try:
        return Decimal(str(value or "0"))
    except (InvalidOperation, ValueError):
        return Decimal(0)


def _integer(value: object) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None
