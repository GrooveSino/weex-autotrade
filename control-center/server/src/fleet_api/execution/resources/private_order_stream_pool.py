"""Short-lived private-stream leases; never shared across account credentials."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from fleet_api.execution.resources.leased_resource_pool import KeyedResourceLeasePool


@dataclass(frozen=True, slots=True)
class PrivateOrderStreamSnapshot:
    active_leases: int
    open_streams: int


class PrivateOrderStreamPool:
    """Keep a stream only while one account has active order verification work."""

    def __init__(self, *, idle_seconds: float = 10) -> None:
        self._pool = KeyedResourceLeasePool(idle_seconds=idle_seconds)

    @contextmanager
    def lease(self, account_key: str, open_stream: Callable[[], Any]) -> Iterator[Any]:
        with self._pool.lease(account_key, open_stream) as resource:
            yield resource

    def snapshot(self) -> PrivateOrderStreamSnapshot:
        active, total, _idle = self._pool.counts()
        return PrivateOrderStreamSnapshot(active_leases=active, open_streams=total)

    def collect(self) -> None:
        self._pool.collect()

    def close(self) -> None:
        self._pool.close()
