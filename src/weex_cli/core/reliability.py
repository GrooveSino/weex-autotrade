from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TypeVar

import ccxt

NETWORK_ERRORS = (ccxt.NetworkError, ccxt.RequestTimeout, ConnectionError, TimeoutError)
_T = TypeVar("_T")
RetrySink = Callable[[Mapping[str, object]], None]


@dataclass(frozen=True)
class ReadRetryPolicy:
    attempts: int = 8
    initial_delay_seconds: float = 1.0
    multiplier: float = 2.0
    max_delay_seconds: float = 8.0

    def __post_init__(self) -> None:
        if self.attempts < 1 or self.initial_delay_seconds < 0 or self.multiplier < 1 or self.max_delay_seconds < 0:
            raise ValueError("read retry policy is invalid")

    def delay_after(self, failed_attempt: int) -> float:
        return min(
            self.max_delay_seconds,
            self.initial_delay_seconds * (self.multiplier ** max(0, failed_attempt - 1)),
        )


DEFAULT_READ_RETRY_POLICY = ReadRetryPolicy()
FAST_READ_RETRY_POLICY = ReadRetryPolicy(attempts=6, initial_delay_seconds=0.25, max_delay_seconds=2.0)


def retry_read(
    reader: Callable[[], _T],
    *,
    operation: str,
    policy: ReadRetryPolicy = DEFAULT_READ_RETRY_POLICY,
    sleep: Callable[[float], None] = time.sleep,
    retry_sink: RetrySink | None = None,
) -> _T:
    """Retry a side-effect-free exchange read; mutations must never use this helper."""

    for attempt in range(1, policy.attempts + 1):
        try:
            return reader()
        except NETWORK_ERRORS as exc:
            if attempt >= policy.attempts:
                raise
            delay = policy.delay_after(attempt)
            if retry_sink is not None:
                retry_sink(
                    {
                        "operation": operation,
                        "failed_attempt": attempt,
                        "next_attempt": attempt + 1,
                        "max_attempts": policy.attempts,
                        "delay_seconds": delay,
                        "error": type(exc).__name__,
                    }
                )
            sleep(delay)
    raise AssertionError("unreachable")
