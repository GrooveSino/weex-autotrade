"""Scalar values used while completing a Campaign actor."""

from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Mapping
from decimal import Decimal
from typing import Any


def ending_available_quote(result: Mapping[str, object]) -> str | None:
    boundary = result.get("final_boundary")
    if not isinstance(boundary, Mapping) or boundary.get("available_quote") is None:
        return None
    try:
        value = Decimal(str(boundary["available_quote"]))
    except Exception:
        return None
    return format(value, "f") if value.is_finite() else None


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def accounting_checkpoint(summaries: list[dict[str, Any]]) -> dict[str, object]:
    commissions: dict[str, Decimal] = defaultdict(Decimal)
    for summary in summaries:
        for asset, value in _mapping(summary.get("commission_by_asset")).items():
            commissions[str(asset)] += _decimal(value)
    return {
        "verified": bool(summaries) and all(bool(row.get("accounting_verified")) for row in summaries),
        "liquidity_policy_satisfied": bool(summaries)
        and all(bool(row.get("liquidity_policy_satisfied", row.get("maker_only"))) for row in summaries),
        "quote_volume": str(sum((_decimal(row.get("quote_volume")) for row in summaries), Decimal(0))),
        "realized_pnl": str(sum((_decimal(row.get("realized_pnl")) for row in summaries), Decimal(0))),
        "fill_count": sum(int(row.get("fill_count") or 0) for row in summaries),
        "maker_count": sum(int(row.get("maker_count") or 0) for row in summaries),
        "taker_count": sum(int(row.get("taker_count") or 0) for row in summaries),
        "unknown_liquidity_count": sum(int(row.get("unknown_liquidity_count") or 0) for row in summaries),
        "commission_by_asset": {asset: str(value) for asset, value in commissions.items()},
    }


def summary_from_checkpoint(checkpoint: object, completed_quote: Decimal) -> list[dict[str, Any]]:
    stored = checkpoint if isinstance(checkpoint, Mapping) else {}
    if completed_quote <= 0:
        return []
    return [
        {
            "quote_volume": str(completed_quote),
            "realized_pnl": str(stored.get("realized_pnl") or "0"),
            "commission_by_asset": dict(_mapping(stored.get("commission_by_asset"))),
            "accounting_verified": bool(stored.get("verified", True)),
            "liquidity_policy_satisfied": bool(stored.get("liquidity_policy_satisfied", True)),
            "fill_count": int(stored.get("fill_count") or 0),
            "maker_count": int(stored.get("maker_count") or 0),
            "taker_count": int(stored.get("taker_count") or 0),
            "unknown_liquidity_count": int(stored.get("unknown_liquidity_count") or 0),
        }
    ]


def _mapping(value: object) -> Mapping[object, object]:
    return value if isinstance(value, Mapping) else {}


def _decimal(value: object) -> Decimal:
    try:
        parsed = Decimal(str(value or "0"))
    except Exception:
        return Decimal(0)
    return parsed if parsed.is_finite() else Decimal(0)
