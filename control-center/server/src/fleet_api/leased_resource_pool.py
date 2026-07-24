"""Thread-safe, keyed resource leases that never hold a lock during I/O."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from threading import Condition, Lock
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _Lease:
    resource: Any
    references: int
    idle_since: float | None = None


class KeyedResourceLeasePool:
    """Share a resource per key while keeping slow opens outside the lock.

    At most one caller creates a resource for a key. Callers for other keys
    keep progressing during that connection attempt, while callers for the
    same key wait for its outcome and share the successfully created resource.
    """

    def __init__(self, *, idle_seconds: float) -> None:
        self._idle_seconds = idle_seconds
        self._leases: dict[str, _Lease] = {}
        self._opening: set[str] = set()
        self._condition = Condition(Lock())
        self._closed = False

    @contextmanager
    def lease(self, key: str, open_resource: Callable[[], Any]) -> Iterator[Any]:
        resource = self._acquire(key, open_resource)
        try:
            yield resource
        finally:
            self._release(key)

    def collect(self) -> None:
        with self._condition:
            stale = self._discard_idle_locked()
        _close_resources(stale)

    def close(self) -> None:
        with self._condition:
            if self._closed:
                return
            self._closed = True
            resources = [lease.resource for lease in self._leases.values()]
            self._leases.clear()
            self._condition.notify_all()
        _close_resources(resources)

    def counts(self) -> tuple[int, int, int]:
        with self._condition:
            active = sum(lease.references for lease in self._leases.values())
            total = len(self._leases)
            idle = sum(lease.references == 0 for lease in self._leases.values())
            return active, total, idle

    def _acquire(self, key: str, open_resource: Callable[[], Any]) -> Any:
        stale: list[Any] = []
        with self._condition:
            stale = self._discard_idle_locked()
            while True:
                self._require_open_locked()
                lease = self._leases.get(key)
                if lease is not None:
                    lease.references += 1
                    lease.idle_since = None
                    break
                if key not in self._opening:
                    self._opening.add(key)
                    lease = None
                    break
                self._condition.wait()

        for resource in stale:
            _close_resource(resource)
        if lease is not None:
            return lease.resource

        try:
            resource = open_resource()
        except Exception:
            with self._condition:
                self._opening.discard(key)
                self._condition.notify_all()
            raise

        with self._condition:
            self._opening.discard(key)
            closed = self._closed
            if not closed:
                self._leases[key] = _Lease(resource, references=1)
            self._condition.notify_all()
        if closed:
            _close_resource(resource)
            raise RuntimeError("resource lease pool closed while opening a connection")
        return resource

    def _release(self, key: str) -> None:
        with self._condition:
            lease = self._leases.get(key)
            if lease is None:
                return
            lease.references = max(0, lease.references - 1)
            if lease.references == 0:
                lease.idle_since = time.monotonic()

    def _discard_idle_locked(self) -> list[Any]:
        now = time.monotonic()
        stale_keys = [
            key
            for key, lease in self._leases.items()
            if lease.references == 0 and lease.idle_since is not None and now - lease.idle_since >= self._idle_seconds
        ]
        return [self._leases.pop(key).resource for key in stale_keys]

    def _require_open_locked(self) -> None:
        if self._closed:
            raise RuntimeError("resource lease pool is closed")


def _close_resource(resource: Any) -> None:
    close = getattr(resource, "close", None)
    if callable(close):
        close()


def _close_resources(resources: list[Any]) -> None:
    for resource in resources:
        try:
            _close_resource(resource)
        except Exception:  # noqa: BLE001 - a failed socket close is non-fatal cleanup
            logger.warning("failed to close an idle leased resource", exc_info=True)
