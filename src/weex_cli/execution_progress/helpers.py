"""Defensive primitive coercions for persisted progress snapshots."""

from __future__ import annotations

from decimal import Decimal
from typing import Any


def _nonnegative_decimal(value: Any) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except Exception:  # noqa: BLE001 - malformed observability values are ignored.
        return None
    return parsed if parsed.is_finite() and parsed >= 0 else None


def _decimal_or_zero(value: Any) -> Decimal:
    return _nonnegative_decimal(value) or Decimal(0)


def _nonnegative_int(value: Any) -> int:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, parsed)
