"""JSON encoding for durable Campaign values received from execution services."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any


def compact_json(value: Any) -> str:
    """Encode execution output without allowing a Decimal to break finalization."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=_decimal_text)


def _decimal_text(value: Any) -> str:
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def json_copy(value: Any) -> Any:
    """Detach mutable execution payloads using the same durable representation."""
    return json.loads(compact_json(value))
