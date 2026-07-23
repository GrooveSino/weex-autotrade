from datetime import UTC, datetime


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def delay_label(seconds: int, action: str) -> str:
    return f"{seconds}s 后{action}"
