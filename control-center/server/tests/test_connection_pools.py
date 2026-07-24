from __future__ import annotations

from fleet_api.market_data_hub import MarketDataHub
from fleet_api.private_order_stream_pool import PrivateOrderStreamPool


class _Resource:
    def __init__(self) -> None:
        self.closed = 0

    def close(self) -> None:
        self.closed += 1


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
