from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from fleet_api.execution.resources.market_data_hub import PublicMarketSnapshotService, SharedMarketUnavailable
from fleet_api.execution.resources.private_order_stream_pool import PrivateOrderStreamPool


class _Resource:
    def __init__(self) -> None:
        self.closed = 0

    def close(self) -> None:
        self.closed += 1


class _FakeGateway(_Resource):
    def __init__(self) -> None:
        super().__init__()
        self.rest_calls = 0

    def order_book(self, symbol: str, _limit: int = 10) -> dict[str, object]:
        self.rest_calls += 1
        return _book(symbol)


class _FakePublicStream(_Resource):
    def __init__(self) -> None:
        super().__init__()
        self.connected = True
        self.started = 0
        self.books = {"BTC": _book("BTC"), "ETH": _book("ETH")}

    def start(self) -> None:
        self.started += 1

    def order_book(self, symbol: str, _limit: int = 5) -> dict[str, object]:
        if not self.connected:
            raise RuntimeError("disconnected")
        return self.books[symbol]


def _book(symbol: str) -> dict[str, object]:
    price = 60_000 if symbol == "BTC" else 2_000
    return {"bids": [[price, 1]], "asks": [[price + 1, 1]], "timestamp": 1, "nonce": 2}


def _wait(predicate) -> None:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + 1
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert predicate()


def test_public_market_has_one_direct_stream_for_many_actor_views() -> None:
    gateways: list[_FakeGateway] = []
    streams: list[_FakePublicStream] = []
    proxy_values: list[str | None] = []

    def gateway_factory(proxy_url: str | None) -> _FakeGateway:
        proxy_values.append(proxy_url)
        gateway = _FakeGateway()
        gateways.append(gateway)
        return gateway

    def stream_factory(_gateway: Any, proxy_url: str | None) -> _FakePublicStream:
        proxy_values.append(proxy_url)
        stream = _FakePublicStream()
        streams.append(stream)
        return stream

    service = PublicMarketSnapshotService(
        enabled=True,
        request_timeout_ms=5_000,
        gateway_factory=gateway_factory,  # type: ignore[arg-type]
        stream_factory=stream_factory,
    )
    service.start()
    try:
        _wait(service.fresh)
        views = [service.actor_view(threading.Event()) for _ in range(200)]
        assert all(view.order_book("BTC")["source"] == "shared_websocket" for view in views)
        assert len(gateways) == len(streams) == 1
        assert proxy_values == [None, None]
        assert gateways[0].rest_calls == 0
        snapshot = service.snapshot()
        assert snapshot.connected and snapshot.generation == 1
        assert snapshot.btc_snapshot_age_ms is not None
        assert snapshot.eth_snapshot_age_ms is not None
    finally:
        service.close()
    assert streams[0].closed == gateways[0].closed == 1


def test_public_market_waiting_does_not_trigger_account_rest_fallback() -> None:
    stream = _FakePublicStream()
    stream.connected = False
    service = PublicMarketSnapshotService(
        enabled=True,
        request_timeout_ms=5_000,
        gateway_factory=lambda _proxy: _FakeGateway(),  # type: ignore[arg-type]
        stream_factory=lambda _gateway, _proxy: stream,
    )
    service.start()
    stop = threading.Event()
    service.set_waiting("execution-1", True)
    try:
        _wait(lambda: service.snapshot().source_state == "disconnected")
        assert not service.fresh()
        stop.set()
        try:
            service.actor_view(stop).order_book("BTC")
        except SharedMarketUnavailable:
            pass
        else:
            raise AssertionError("stopped shared-market wait must not return a stale book")
        assert service.snapshot().waiting_phase_count == 1
    finally:
        service.set_waiting("execution-1", False)
        service.close()


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


def test_private_stream_slow_creation_isolated_by_account() -> None:
    pool = PrivateOrderStreamPool(idle_seconds=60)
    opening = threading.Event()
    allow = threading.Event()

    def slow_open() -> _Resource:
        opening.set()
        assert allow.wait(timeout=1)
        return _Resource()

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(lambda: _lease(pool, "account-a", slow_open))
        assert opening.wait(timeout=1)
        second = executor.submit(lambda: _lease(pool, "account-b", _Resource))
        assert isinstance(second.result(timeout=0.5), _Resource)
        allow.set()
        first.result(timeout=1)
    pool.close()


def _lease(pool: PrivateOrderStreamPool, key: str, factory):  # type: ignore[no-untyped-def]
    with pool.lease(key, factory) as resource:
        return resource
