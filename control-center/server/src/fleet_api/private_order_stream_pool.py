"""Short-lived private-stream leases; never shared across account credentials."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from threading import Lock
from typing import Any


@dataclass(slots=True)
class _PrivateLease:
    resource: Any
    references: int
    idle_since: float | None = None


@dataclass(frozen=True, slots=True)
class PrivateOrderStreamSnapshot:
    active_leases: int
    open_streams: int


class PrivateOrderStreamPool:
    """Keep a stream only while one account has active order verification work."""

    def __init__(self, *, idle_seconds: float = 10) -> None:
        self._idle_seconds = idle_seconds
        self._leases: dict[str, _PrivateLease] = {}
        self._lock = Lock()

    @contextmanager
    def lease(self, account_key: str, open_stream: Callable[[], Any]) -> Iterator[Any]:
        with self._lock:
            self._collect_locked()
            lease = self._leases.get(account_key)
            if lease is None:
                lease = _PrivateLease(open_stream(), 0)
                self._leases[account_key] = lease
            lease.references += 1
            lease.idle_since = None
        try:
            yield lease.resource
        finally:
            with self._lock:
                current = self._leases.get(account_key)
                if current is not None:
                    current.references -= 1
                    if current.references == 0:
                        current.idle_since = time.monotonic()

    def snapshot(self) -> PrivateOrderStreamSnapshot:
        with self._lock:
            return PrivateOrderStreamSnapshot(
                active_leases=sum(lease.references for lease in self._leases.values()),
                open_streams=len(self._leases),
            )

    def collect(self) -> None:
        with self._lock:
            self._collect_locked()

    def close(self) -> None:
        with self._lock:
            leases = tuple(self._leases.values())
            self._leases.clear()
        for lease in leases:
            _close_resource(lease.resource)

    def _collect_locked(self) -> None:
        now = time.monotonic()
        stale = [
            key
            for key, lease in self._leases.items()
            if lease.references == 0 and lease.idle_since is not None and now - lease.idle_since >= self._idle_seconds
        ]
        for key in stale:
            lease = self._leases.pop(key)
            _close_resource(lease.resource)


def _close_resource(resource: Any) -> None:
    close = getattr(resource, "close", None)
    if callable(close):
        close()
