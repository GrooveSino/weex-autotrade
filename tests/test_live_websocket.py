from __future__ import annotations

import base64
import hashlib
import hmac

import pytest

from weex_cli import live_websocket
from weex_cli.core.config import Credentials
from weex_cli.live_websocket import (
    MarketStreamUnavailable,
    WeexPrivateOrderStream,
    WeexPublicOrderBookStream,
)


class SnapshotGateway:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.update_id = 100

    def order_book(self, symbol: str, limit: int = 15) -> dict:
        self.calls.append(symbol)
        return {
            "bids": [[100, 2], [99, 3]],
            "asks": [[101, 4], [102, 5]],
            "nonce": self.update_id,
        }


class Socket:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, message: str) -> None:
        self.sent.append(message)


def test_public_depth_stream_bootstraps_applies_deltas_and_serves_fresh_bbo() -> None:
    gateway = SnapshotGateway()
    clock = [10.0]
    stream = WeexPublicOrderBookStream(gateway, monotonic=lambda: clock[0])
    socket = Socket()
    stream._mark_connected()

    stream.handle_message(
        socket,
        '{"e":"depth","E":1234,"s":"BTCUSDT","U":101,"u":101,"b":[["100","0"],["100.5","1.5"]],"a":[["101","2.5"]]}',
    )

    book = stream.order_book("BTC", 2)
    assert gateway.calls == ["BTC"]
    assert book["bids"] == [[100.5, 1.5], [99.0, 3.0]]
    assert book["asks"] == [[101.0, 2.5], [102.0, 5.0]]
    assert book["nonce"] == 101
    assert book["source"] == "websocket"


def test_public_depth_stream_resnapshots_after_sequence_gap() -> None:
    gateway = SnapshotGateway()
    stream = WeexPublicOrderBookStream(gateway, monotonic=lambda: 10.0)
    socket = Socket()
    stream._mark_connected()
    stream.handle_message(socket, '{"e":"depth","s":"BTCUSDT","U":101,"u":101,"b":[],"a":[]}')

    gateway.update_id = 102
    stream.handle_message(
        socket,
        '{"e":"depth","s":"BTCUSDT","U":103,"u":103,"b":[["100.8","1"]],"a":[]}',
    )

    assert gateway.calls == ["BTC", "BTC"]
    assert stream.order_book("BTC")["nonce"] == 103


def test_public_depth_stream_rejects_stale_or_disconnected_cache() -> None:
    gateway = SnapshotGateway()
    clock = [10.0]
    stream = WeexPublicOrderBookStream(gateway, monotonic=lambda: clock[0], max_age_seconds=2)
    socket = Socket()
    stream._mark_connected()
    stream.handle_message(socket, '{"e":"depth","s":"BTCUSDT","U":101,"u":101,"b":[],"a":[]}')

    clock[0] = 12.1
    with pytest.raises(MarketStreamUnavailable, match="stale"):
        stream.order_book("BTC")
    stream._mark_disconnected()
    with pytest.raises(MarketStreamUnavailable, match="not synchronized"):
        stream.order_book("BTC")


def test_public_depth_stream_handles_business_ping_and_subscription_failure() -> None:
    stream = WeexPublicOrderBookStream(SnapshotGateway())
    socket = Socket()

    stream.handle_message(socket, '{"event":"ping","time":"123"}')
    assert socket.sent == ['{"method":"PONG","id":1}']
    with pytest.raises(MarketStreamUnavailable, match="denied"):
        stream.handle_message(socket, '{"result":false,"id":1,"msg":"denied"}')


def test_socks_stream_without_adapter_uses_rest_fallback_without_retry_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        live_websocket.importlib.util, "find_spec", lambda name: None if name == "python_socks" else None
    )
    stream = WeexPublicOrderBookStream(SnapshotGateway(), proxy_url="socks5://127.0.0.1:1080")

    stream.start()

    assert stream.connected is False
    assert stream._thread is None


def test_private_stream_signs_headers_and_caches_order_updates() -> None:
    credentials = Credentials(api_key="key", api_secret="secret", passphrase="pass")
    stream = WeexPrivateOrderStream(credentials, clock_ms=lambda: 1234567890)
    socket = Socket()
    expected = base64.b64encode(hmac.new(b"secret", b"1234567890/v3/ws/private", hashlib.sha256).digest()).decode()

    assert stream.headers()["ACCESS-SIGN"] == expected
    stream._mark_connected()
    stream.handle_message(
        socket,
        '{"e":"orders","d":[{"id":"o-1","clientOrderId":"c-1","orderSide":"BUY",'
        '"status":"FILLED","size":"0.2","cumFillSize":"0.2","cumFillValue":"20",'
        '"price":"100","timeInForce":"POST_ONLY"}]}',
    )

    row = stream.order_update("o-1", "")
    assert row is not None
    assert row["status"] == "filled"
    assert row["filled"] == "0.2"
    assert row["postOnly"] is True
    stream._mark_disconnected()
    assert stream.order_update("o-1", "c-1") is None


def test_private_stream_handles_business_ping() -> None:
    stream = WeexPrivateOrderStream(Credentials("key", "secret", "pass"))
    socket = Socket()

    stream.handle_message(socket, '{"type":"ping","time":"123"}')

    assert socket.sent == ['{"method":"PONG","id":1}']
