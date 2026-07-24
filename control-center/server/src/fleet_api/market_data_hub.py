"""Reference-counted public market-data leases grouped by proxy exit."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from threading import Lock
from typing import Any


@dataclass(slots=True)
class _Lease:
    resource: Any
    references: int
    idle_since: float | None = None


@dataclass(frozen=True, slots=True)
class MarketDataHubSnapshot:
    active_leases: int
    shared_connections: int
    idle_connections: int


class MarketDataHub:
    """Share public BTC/ETH streams only for the same durable proxy identity."""

    def __init__(self, *, idle_seconds: float = 30) -> None:
        self._idle_seconds = idle_seconds
        self._leases: dict[str, _Lease] = {}
        self._lock = Lock()

    @contextmanager
    def lease(self, proxy_key: str, open_stream: Callable[[], Any]) -> Iterator[Any]:
        with self._lock:
            self._collect_locked()
            lease = self._leases.get(proxy_key)
            if lease is None:
                lease = _Lease(open_stream(), 0)
                self._leases[proxy_key] = lease
            lease.references += 1
            lease.idle_since = None
        try:
            yield lease.resource
        finally:
            with self._lock:
                current = self._leases.get(proxy_key)
                if current is not None:
                    current.references -= 1
                    if current.references == 0:
                        current.idle_since = time.monotonic()

    def collect(self) -> None:
        with self._lock:
            self._collect_locked()

    def close(self) -> None:
        with self._lock:
            leases = tuple(self._leases.values())
            self._leases.clear()
        for lease in leases:
            _close_resource(lease.resource)

    def snapshot(self) -> MarketDataHubSnapshot:
        with self._lock:
            active = sum(lease.references for lease in self._leases.values())
            idle = sum(lease.references == 0 for lease in self._leases.values())
            return MarketDataHubSnapshot(active, len(self._leases), idle)

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
