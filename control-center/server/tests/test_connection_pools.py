from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from fleet_api.market_data_hub import MarketDataHub
from fleet_api.private_order_stream_pool import PrivateOrderStreamPool


class _Resource:
    def __init__(self) -> None:
        self.closed = 0

    def close(self) -> None:
        self.closed += 1


class _FailingResource(_Resource):
    def close(self) -> None:
        super().close()
        raise RuntimeError("close failed")


def test_market_data_leases_share_only_the_same_proxy_resource() -> None:
    hub = MarketDataHub(idle_seconds=60)
    opened: list[_Resource] = []

    def open_stream() -> _Resource:
        resource = _Resource()
        opened.append(resource)
        return resource

    with hub.lease("proxy-a", open_stream) as first, hub.lease("proxy-a", open_stream) as second:
        assert first is second
        assert hub.snapshot().active_leases == 2
        assert hub.snapshot().shared_connections == 1
    with hub.lease("proxy-b", open_stream):
        assert hub.snapshot().shared_connections == 2
    hub.close()

    assert len(opened) == 2
    assert all(resource.closed == 1 for resource in opened)


def test_private_order_leases_are_isolated_per_account() -> None:
    pool = PrivateOrderStreamPool(idle_seconds=60)
    opened: list[_Resource] = []

    def open_stream() -> _Resource:
        resource = _Resource()
        opened.append(resource)
        return resource

    with pool.lease("account-a", open_stream) as first, pool.lease("account-a", open_stream) as second:
        assert first is second
        with pool.lease("account-b", open_stream) as other:
            assert other is not first
            assert pool.snapshot().open_streams == 2
    pool.close()

    assert len(opened) == 2
    assert all(resource.closed == 1 for resource in opened)


@pytest.mark.parametrize(
    ("pool_factory", "key"),
    [(MarketDataHub, "proxy-a"), (PrivateOrderStreamPool, "account-a")],
)
def test_idle_resource_is_closed_by_periodic_collection(pool_factory, key: str) -> None:
    pool = pool_factory(idle_seconds=0)
    resource = _Resource()

    with pool.lease(key, lambda: resource):
        pass

    pool.collect()

    assert resource.closed == 1


@pytest.mark.parametrize(
    ("pool_factory", "first_key", "second_key"),
    [
        (MarketDataHub, "proxy-a", "proxy-b"),
        (PrivateOrderStreamPool, "account-a", "account-b"),
    ],
)
def test_collection_continues_when_one_idle_resource_close_fails(pool_factory, first_key: str, second_key: str) -> None:
    pool = pool_factory(idle_seconds=0)
    failing = _FailingResource()
    healthy = _Resource()

    with pool.lease(first_key, lambda: failing), pool.lease(second_key, lambda: healthy):
        pass

    pool.collect()

    assert failing.closed == 1
    assert healthy.closed == 1


@pytest.mark.parametrize(
    ("pool_factory", "first_key", "second_key"),
    [
        (MarketDataHub, "proxy-a", "proxy-b"),
        (PrivateOrderStreamPool, "account-a", "account-b"),
    ],
)
def test_slow_connection_creation_for_one_key_does_not_block_another_key(
    pool_factory,
    first_key: str,
    second_key: str,
) -> None:
    pool = pool_factory(idle_seconds=60)
    opening = Event()
    allow_first_connection = Event()

    def slow_open() -> _Resource:
        opening.set()
        assert allow_first_connection.wait(timeout=1)
        return _Resource()

    def first() -> None:
        with pool.lease(first_key, slow_open):
            pass

    def second() -> _Resource:
        with pool.lease(second_key, _Resource) as resource:
            return resource

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(first)
        assert opening.wait(timeout=1)
        second_future = executor.submit(second)
        second_resource = second_future.result(timeout=0.5)
        allow_first_connection.set()
        first_future.result(timeout=1)

    pool.close()
    assert second_resource.closed == 1


@pytest.mark.parametrize(
    ("pool_factory", "key"),
    [(MarketDataHub, "proxy-a"), (PrivateOrderStreamPool, "account-a")],
)
def test_same_key_waiters_share_one_opening_connection(pool_factory, key: str) -> None:
    pool = pool_factory(idle_seconds=60)
    opening = Event()
    allow_connection = Event()
    opened: list[_Resource] = []

    def open_stream() -> _Resource:
        opening.set()
        assert allow_connection.wait(timeout=1)
        resource = _Resource()
        opened.append(resource)
        return resource

    def lease_resource() -> _Resource:
        with pool.lease(key, open_stream) as resource:
            return resource

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(lease_resource)
        assert opening.wait(timeout=1)
        second = executor.submit(lease_resource)
        allow_connection.set()
        assert first.result(timeout=1) is second.result(timeout=1)

    pool.close()
    assert len(opened) == 1
    assert opened[0].closed == 1
