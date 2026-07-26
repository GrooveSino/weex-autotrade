"""WEEX public depth stream with REST bootstrap and sequence validation."""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from weex_cli.core.symbols import base_asset, live_symbol_id
from weex_cli.presentation.i18n import text

from .contracts import (
    DEFAULT_BOOK_MAX_AGE_SECONDS,
    WEEX_PUBLIC_WS_URL,
    DepthState,
    MarketStreamUnavailable,
    OrderBookGateway,
)
from .helpers import (
    apply_levels,
    depth_levels,
    json_payload,
    positive_int,
    retry_delay,
    should_log_reconnect_error,
    snapshot_update_id,
    socks_proxy_needs_unavailable_dependency,
    websocket_connect,
)

LOGGER = logging.getLogger(__name__)


class WeexPublicOrderBookStream:
    """Maintain synchronized WEEX depth books over one public WebSocket."""

    def __init__(
        self,
        snapshot_gateway: OrderBookGateway,
        symbols: tuple[str, ...] = ("BTC", "ETH"),
        *,
        url: str = WEEX_PUBLIC_WS_URL,
        proxy_url: str | None = None,
        connect_factory: Callable[..., Any] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        max_age_seconds: float = DEFAULT_BOOK_MAX_AGE_SECONDS,
    ) -> None:
        self.snapshot_gateway = snapshot_gateway
        self.symbols = tuple(live_symbol_id(symbol) for symbol in symbols)
        self.url = url
        self.proxy_url = proxy_url
        self.connect_factory = connect_factory or websocket_connect
        self.monotonic = monotonic
        self.max_age_seconds = max_age_seconds
        self._books = {symbol: DepthState() for symbol in self.symbols}
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._connected = False
        self._consecutive_errors = 0

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        if socks_proxy_needs_unavailable_dependency(self.proxy_url):
            LOGGER.info(
                text(
                    "WEEX 公共 WebSocket 使用 SOCKS 代理但同步适配器不可用；将安全回退到 REST",
                    "WEEX public WebSocket SOCKS adapter unavailable; safely falling back to REST",
                )
            )
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="weex-public-depth", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
        self._mark_disconnected()

    def wait_ready(self, timeout: float = 0) -> bool:
        return self._ready.wait(timeout)

    @property
    def connected(self) -> bool:
        with self._lock:
            return self._connected

    def subscription_message(self) -> str:
        return json.dumps(
            {"method": "SUBSCRIBE", "params": [f"{symbol}@depth15" for symbol in self.symbols], "id": 1},
            separators=(",", ":"),
        )

    def order_book(self, symbol: str, limit: int = 5) -> dict[str, Any]:
        symbol_id = live_symbol_id(symbol)
        with self._lock:
            state = self._books.get(symbol_id)
            if not self._connected or state is None or state.update_id is None:
                raise MarketStreamUnavailable(f"WEEX WebSocket book is not synchronized for {symbol_id}")
            age = self.monotonic() - state.received_at
            if age < 0 or age > self.max_age_seconds:
                raise MarketStreamUnavailable(f"WEEX WebSocket book is stale for {symbol_id}")
            bids = sorted(state.bids.items(), reverse=True)[:limit]
            asks = sorted(state.asks.items())[:limit]
            if not bids or not asks or bids[0][0] >= asks[0][0]:
                raise MarketStreamUnavailable(f"WEEX WebSocket book is invalid for {symbol_id}")
            return {
                "bids": [[float(price), float(size)] for price, size in bids],
                "asks": [[float(price), float(size)] for price, size in asks],
                "timestamp": state.event_time_ms,
                "nonce": state.update_id,
                "source": "websocket",
            }

    def handle_message(self, websocket: Any, raw_message: str | bytes) -> None:
        payload = json_payload(raw_message)
        if payload is None:
            return
        if payload.get("event") == "ping":
            websocket.send('{"method":"PONG","id":1}')
            return
        if "result" in payload:
            if payload.get("result") is not True:
                raise MarketStreamUnavailable(
                    f"WEEX depth subscription failed: {payload.get('msg') or 'unknown error'}"
                )
            self._consecutive_errors = 0
            return
        if payload.get("e") != "depth":
            return
        symbol = str(payload.get("s") or "").upper()
        if symbol not in self._books:
            return
        first_id, last_id = positive_int(payload.get("U")), positive_int(payload.get("u"))
        if first_id is None or last_id is None or first_id > last_id:
            raise MarketStreamUnavailable(f"WEEX depth update IDs are invalid for {symbol}")
        with self._lock:
            current_id = self._books[symbol].update_id
        if current_id is None:
            self._bootstrap(symbol)
            with self._lock:
                current_id = self._books[symbol].update_id
        assert current_id is not None
        if last_id <= current_id:
            return
        if not first_id <= current_id + 1 <= last_id:
            self._bootstrap(symbol)
            with self._lock:
                current_id = self._books[symbol].update_id
            assert current_id is not None
            if last_id <= current_id:
                return
            if not first_id <= current_id + 1 <= last_id:
                raise MarketStreamUnavailable(f"WEEX depth sequence gap for {symbol}")
        with self._lock:
            state = self._books[symbol]
            apply_levels(state.bids, depth_levels(payload.get("b")))
            apply_levels(state.asks, depth_levels(payload.get("a")))
            state.update_id = last_id
            state.event_time_ms = positive_int(payload.get("E"))
            state.received_at = self.monotonic()
            self._trim(state)
            self._update_ready_locked()
            self._consecutive_errors = 0

    def _bootstrap(self, symbol: str) -> None:
        snapshot = self.snapshot_gateway.order_book(base_asset(symbol), 15)
        bids, asks, update_id = (
            depth_levels(snapshot.get("bids")),
            depth_levels(snapshot.get("asks")),
            snapshot_update_id(snapshot),
        )
        if update_id is None or not bids or not asks:
            raise MarketStreamUnavailable(f"WEEX REST depth snapshot is incomplete for {symbol}")
        with self._lock:
            self._books[symbol] = DepthState(
                {price: size for price, size in bids if size > 0},
                {price: size for price, size in asks if size > 0},
                update_id,
                positive_int(snapshot.get("timestamp")),
                self.monotonic(),
            )
            self._trim(self._books[symbol])
            self._update_ready_locked()

    @staticmethod
    def _trim(state: DepthState) -> None:
        state.bids = dict(sorted(state.bids.items(), reverse=True)[:200])
        state.asks = dict(sorted(state.asks.items())[:200])

    def _update_ready_locked(self) -> None:
        if all(state.update_id is not None for state in self._books.values()):
            self._ready.set()

    def _mark_connected(self) -> None:
        with self._lock:
            self._connected = True

    def _mark_disconnected(self) -> None:
        with self._lock:
            self._connected = False
            self._ready.clear()
            self._books = {symbol: DepthState() for symbol in self.symbols}

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                with self.connect_factory(
                    self.url, open_timeout=15, close_timeout=5, ping_interval=None, proxy=self.proxy_url
                ) as websocket:
                    self._mark_connected()
                    websocket.send(self.subscription_message())
                    while not self._stop.is_set():
                        try:
                            self.handle_message(websocket, websocket.recv(timeout=1))
                        except TimeoutError:
                            continue
            except Exception as exc:  # noqa: BLE001 - REST remains the read fallback
                self._consecutive_errors += 1
                if should_log_reconnect_error(self._consecutive_errors):
                    LOGGER.warning(
                        text("WEEX 公共 WebSocket 暂不可用：%s", "WEEX public WebSocket unavailable: %s"),
                        type(exc).__name__,
                    )
            finally:
                self._mark_disconnected()
            self._stop.wait(retry_delay(self._consecutive_errors))
