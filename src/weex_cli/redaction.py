from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

_ASSIGNMENT_RE = re.compile(
    r"(?i)(api[_-]?key|secret|passphrase|password|u[_-]?token|cc[_-]?token|r[_-]?token|terminal[_-]?code)"
    r"\s*[:=]\s*([^\s,;}]+)"
)


def _is_sensitive_key(value: object) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", str(value).lower())
    return (
        normalized.endswith("apikey")
        or normalized in {"accesskey", "accesssign", "token", "utoken", "cctoken", "rtoken", "terminalcode"}
        or any(token in normalized for token in ("secret", "passphrase", "password"))
    )


def redact_text(value: object) -> str:
    return _ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}=[REDACTED]", str(value))


def redact(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: "[REDACTED]" if _is_sensitive_key(key) else redact(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value
