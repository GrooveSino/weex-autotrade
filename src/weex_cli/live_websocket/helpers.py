"""Protocol parsing and connection helpers for WEEX WebSocket streams."""

from __future__ import annotations

import importlib.util
import json
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any


def json_payload(raw_message: str | bytes) -> dict[str, Any] | None:
    if isinstance(raw_message, bytes):
        raw_message = raw_message.decode("utf-8")
    payload = json.loads(raw_message)
    return payload if isinstance(payload, dict) else None


def depth_levels(value: Any) -> list[tuple[Decimal, Decimal]]:
    if not isinstance(value, list):
        return []
    levels: list[tuple[Decimal, Decimal]] = []
    for row in value:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        try:
            price = Decimal(str(row[0]))
            size = Decimal(str(row[1]))
        except (InvalidOperation, TypeError, ValueError):
            continue
        if not price.is_finite() or not size.is_finite() or price <= 0 or size < 0:
            continue
        levels.append((price, size))
    return levels


def apply_levels(book: dict[Decimal, Decimal], levels: list[tuple[Decimal, Decimal]]) -> None:
    for price, size in levels:
        if size == 0:
            book.pop(price, None)
        else:
            book[price] = size


def snapshot_update_id(snapshot: Mapping[str, Any]) -> int | None:
    direct = positive_int(snapshot.get("lastUpdateId")) or positive_int(snapshot.get("nonce"))
    if direct is not None:
        return direct
    info = snapshot.get("info")
    return positive_int(info.get("lastUpdateId")) if isinstance(info, Mapping) else None


def positive_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def retry_delay(consecutive_errors: int) -> float:
    if consecutive_errors <= 0:
        return 0.0
    return min(30.0, float(2 ** min(consecutive_errors - 1, 5)))


def should_log_reconnect_error(consecutive_errors: int) -> bool:
    return consecutive_errors in {1, 2, 4, 8, 16, 32}


def socks_proxy_needs_unavailable_dependency(proxy_url: str | None) -> bool:
    if not proxy_url or not proxy_url.lower().startswith("socks"):
        return False
    return importlib.util.find_spec("python_socks") is None


def websocket_connect(url: str, **kwargs: Any) -> Any:
    from websockets.sync.client import connect

    return connect(url, **kwargs)
