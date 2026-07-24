"""Reference-counted public market-data leases grouped by proxy exit."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from .leased_resource_pool import KeyedResourceLeasePool


@dataclass(frozen=True, slots=True)
class MarketDataHubSnapshot:
    active_leases: int
    shared_connections: int
    idle_connections: int


class MarketDataHub:
    """Share public BTC/ETH streams only for the same durable proxy identity."""

    def __init__(self, *, idle_seconds: float = 30) -> None:
        self._pool = KeyedResourceLeasePool(idle_seconds=idle_seconds)

    @contextmanager
    def lease(self, proxy_key: str, open_stream: Callable[[], Any]) -> Iterator[Any]:
        with self._pool.lease(proxy_key, open_stream) as resource:
            yield resource

    def collect(self) -> None:
        self._pool.collect()

    def close(self) -> None:
        self._pool.close()

    def snapshot(self) -> MarketDataHubSnapshot:
        active, total, idle = self._pool.counts()
        return MarketDataHubSnapshot(active, total, idle)
