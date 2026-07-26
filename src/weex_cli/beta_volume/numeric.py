"""Finite decimal parsing for exchange payload values."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from weex_cli.core.errors import ValidationError


def decimal_from_exchange(value: Any, name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except Exception as exc:  # noqa: BLE001 - normalize exchange payload validation
        raise ValidationError(f"WEEX {name} is not numeric") from exc
    if not result.is_finite():
        raise ValidationError(f"WEEX {name} is not finite")
    return result


_decimal = decimal_from_exchange
