"""Shared formatting and extraction helpers for credential-free reports."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any


def mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value or 0))
    except InvalidOperation:
        return Decimal("0")


def float_value(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def int_value(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def text(value: Any) -> str:
    return "unknown" if value is None or value == "" else str(value)


def escape(value: Any) -> str:
    return text(value).replace("|", "\\|").replace("\n", " ")


def number(value: Decimal) -> str:
    return f"{value:f}"


def seconds(value: float | None) -> str:
    return "未提供" if value is None else f"{value:.3f} s"


def delta(delta_value: float | None, percent: float | None, *, comparable: bool) -> str:
    if not comparable:
        return "不可比（本轮未完成）"
    if delta_value is None or percent is None:
        return "未比较"
    direction = "更快" if delta_value > 0 else "更慢"
    return f"{abs(delta_value):.3f} s {direction}（{abs(percent):.1f}%）"


def passed(value: bool) -> str:
    return "通过" if value else "失败"
