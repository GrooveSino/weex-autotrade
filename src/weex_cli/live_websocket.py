from __future__ import annotations

import base64
import hashlib
import hmac
import importlib.util
import json
import logging
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from weex_cli.config import Credentials
from weex_cli.i18n import text
from weex_cli.symbols import base_asset, live_symbol_id

LOGGER = logging.getLogger(__name__)

WEEX_PUBLIC_WS_URL = "wss://ws-contract.weex.com/v3/ws/public"
WEEX_PRIVATE_WS_URL = "wss://ws-contract.weex.com/v3/ws/private"
WEEX_PRIVATE_WS_PATH = "/v3/ws/private"
DEFAULT_BOOK_MAX_AGE_SECONDS = 3.0


class OrderBookGateway(Protocol):
    def order_book(self, symbol: str, limit: int = 15) -> dict[str, Any]: ...


class MarketStreamUnavailable(RuntimeError):
    """The cached WebSocket book cannot safely serve a quote."""


@dataclass
class _DepthState:
    bids: dict[Decimal, Decimal] = field(default_factory=dict)
    asks: dict[Decimal, Decimal] = field(default_factory=dict)
    update_id: int | None = None
    event_time_ms: int | None = None
    received_at: float = 0.0


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
        self.connect_factory = connect_factory or _websocket_connect
        self.monotonic = monotonic
        self.max_age_seconds = max_age_seconds
        self._books = {symbol: _DepthState() for symbol in self.symbols}
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._connected = False
        self._consecutive_errors = 0

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        if _socks_proxy_needs_unavailable_dependency(self.proxy_url):
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
        thread = self._thread
        if thread is not None:
            thread.join(timeout=3)
        self._mark_disconnected()

    def wait_ready(self, timeout: float = 0) -> bool:
        return self._ready.wait(timeout)

    @property
    def connected(self) -> bool:
        with self._lock:
            return self._connected

    def subscription_message(self) -> str:
        return json.dumps(
            {
                "method": "SUBSCRIBE",
                "params": [f"{symbol}@depth15" for symbol in self.symbols],
                "id": 1,
            },
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
        payload = _json_payload(raw_message)
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
        first_id = _positive_int(payload.get("U"))
        last_id = _positive_int(payload.get("u"))
        if first_id is None or last_id is None or first_id > last_id:
            raise MarketStreamUnavailable(f"WEEX depth update IDs are invalid for {symbol}")

        with self._lock:
            state = self._books[symbol]
            current_id = state.update_id
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

        bids = _depth_levels(payload.get("b"))
        asks = _depth_levels(payload.get("a"))
        with self._lock:
            state = self._books[symbol]
            _apply_levels(state.bids, bids)
            _apply_levels(state.asks, asks)
            state.update_id = last_id
            state.event_time_ms = _positive_int(payload.get("E"))
            state.received_at = self.monotonic()
            self._trim(state)
            self._update_ready_locked()
            self._consecutive_errors = 0

    def _bootstrap(self, symbol: str) -> None:
        snapshot = self.snapshot_gateway.order_book(base_asset(symbol), 15)
        bids = _depth_levels(snapshot.get("bids"))
        asks = _depth_levels(snapshot.get("asks"))
        update_id = _snapshot_update_id(snapshot)
        if update_id is None or not bids or not asks:
            raise MarketStreamUnavailable(f"WEEX REST depth snapshot is incomplete for {symbol}")
        with self._lock:
            self._books[symbol] = _DepthState(
                bids={price: size for price, size in bids if size > 0},
                asks={price: size for price, size in asks if size > 0},
                update_id=update_id,
                event_time_ms=_positive_int(snapshot.get("timestamp")),
                received_at=self.monotonic(),
            )
            self._trim(self._books[symbol])
            self._update_ready_locked()

    def _trim(self, state: _DepthState) -> None:
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
            self._books = {symbol: _DepthState() for symbol in self.symbols}

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                with self.connect_factory(
                    self.url,
                    open_timeout=15,
                    close_timeout=5,
                    ping_interval=None,
                    # A configured account proxy is the only proxy this runtime
                    # may use.  `True` makes websockets inspect process-global
                    # proxy environment variables, which is both surprising for
                    # a "no proxy" account and can pull in an unsupported SOCKS
                    # adapter at runtime.
                    proxy=self.proxy_url,
                ) as websocket:
                    self._mark_connected()
                    websocket.send(self.subscription_message())
                    while not self._stop.is_set():
                        try:
                            message = websocket.recv(timeout=1)
                        except TimeoutError:
                            continue
                        self.handle_message(websocket, message)
            except Exception as exc:  # noqa: BLE001 - reads fall back to REST while reconnecting
                self._consecutive_errors += 1
                if _should_log_reconnect_error(self._consecutive_errors):
                    LOGGER.warning(
                        text("WEEX 公共 WebSocket 暂不可用：%s", "WEEX public WebSocket unavailable: %s"),
                        type(exc).__name__,
                    )
            finally:
                self._mark_disconnected()
            self._stop.wait(_retry_delay(self._consecutive_errors))


class WeexPrivateOrderStream:
    """Cache private order lifecycle updates while REST remains authoritative."""

    def __init__(
        self,
        credentials: Credentials,
        *,
        url: str = WEEX_PRIVATE_WS_URL,
        proxy_url: str | None = None,
        connect_factory: Callable[..., Any] | None = None,
        clock_ms: Callable[[], int] = lambda: int(time.time() * 1000),
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.credentials = credentials
        self.url = url
        self.proxy_url = proxy_url
        self.connect_factory = connect_factory or _websocket_connect
        self.clock_ms = clock_ms
        self.monotonic = monotonic
        self._orders_by_id: dict[str, dict[str, Any]] = {}
        self._orders_by_client_id: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._connected = False
        self._consecutive_errors = 0

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        if _socks_proxy_needs_unavailable_dependency(self.proxy_url):
            LOGGER.info(
                text(
                    "WEEX 私有 WebSocket 使用 SOCKS 代理但同步适配器不可用；将安全回退到 REST",
                    "WEEX private WebSocket SOCKS adapter unavailable; safely falling back to REST",
                )
            )
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="weex-private-orders", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=3)
        self._mark_disconnected()

    def order_update(self, order_id: str, client_order_id: str) -> Mapping[str, Any] | None:
        with self._lock:
            if not self._connected:
                return None
            row = self._orders_by_id.get(order_id) or self._orders_by_client_id.get(client_order_id)
            return dict(row) if row is not None else None

    def subscription_message(self) -> str:
        return json.dumps(
            {"method": "SUBSCRIBE", "params": ["orders", "fill", "positions"], "id": 1},
            separators=(",", ":"),
        )

    def headers(self) -> dict[str, str]:
        timestamp = str(self.clock_ms())
        message = f"{timestamp}{WEEX_PRIVATE_WS_PATH}".encode()
        digest = hmac.new(self.credentials.api_secret.encode(), message, hashlib.sha256).digest()
        return {
            "User-Agent": "weex-autotrade/0.1",
            "ACCESS-KEY": self.credentials.api_key,
            "ACCESS-PASSPHRASE": self.credentials.passphrase,
            "ACCESS-TIMESTAMP": timestamp,
            "ACCESS-SIGN": base64.b64encode(digest).decode(),
        }

    def handle_message(self, websocket: Any, raw_message: str | bytes) -> None:
        payload = _json_payload(raw_message)
        if payload is None:
            return
        if payload.get("type") == "ping":
            websocket.send('{"method":"PONG","id":1}')
            return
        if "result" in payload:
            if payload.get("result") is not True:
                raise MarketStreamUnavailable(
                    f"WEEX private subscription failed: {payload.get('msg') or 'unknown error'}"
                )
            self._consecutive_errors = 0
            return
        if payload.get("e") != "orders":
            return
        rows = payload.get("d")
        if not isinstance(rows, list):
            return
        with self._lock:
            for raw_row in rows:
                if not isinstance(raw_row, Mapping):
                    continue
                row = _normalized_order_update(raw_row)
                order_id = str(row.get("id") or "")
                client_id = str(row.get("clientOrderId") or "")
                if order_id:
                    self._orders_by_id[order_id] = row
                if client_id:
                    self._orders_by_client_id[client_id] = row
            self._consecutive_errors = 0

    def _mark_connected(self) -> None:
        with self._lock:
            self._connected = True

    def _mark_disconnected(self) -> None:
        with self._lock:
            self._connected = False
            self._orders_by_id.clear()
            self._orders_by_client_id.clear()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                with self.connect_factory(
                    self.url,
                    open_timeout=15,
                    close_timeout=5,
                    ping_interval=None,
                    proxy=self.proxy_url,
                    additional_headers=self.headers(),
                ) as websocket:
                    self._mark_connected()
                    websocket.send(self.subscription_message())
                    while not self._stop.is_set():
                        try:
                            message = websocket.recv(timeout=1)
                        except TimeoutError:
                            continue
                        self.handle_message(websocket, message)
            except Exception as exc:  # noqa: BLE001 - order reads fall back to REST while reconnecting
                self._consecutive_errors += 1
                if _should_log_reconnect_error(self._consecutive_errors):
                    LOGGER.warning(
                        text("WEEX 私有 WebSocket 暂不可用：%s", "WEEX private WebSocket unavailable: %s"),
                        type(exc).__name__,
                    )
            finally:
                self._mark_disconnected()
            self._stop.wait(_retry_delay(self._consecutive_errors))


class WeexCampaignWebSocketRuntime:
    """Campaign-scoped public and private WebSocket connections."""

    def __init__(
        self,
        snapshot_gateway: OrderBookGateway,
        credentials: Credentials,
        *,
        proxy_url: str | None = None,
        public_connect_factory: Callable[..., Any] | None = None,
        private_connect_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.public = WeexPublicOrderBookStream(
            snapshot_gateway,
            proxy_url=proxy_url,
            connect_factory=public_connect_factory,
        )
        self.private = WeexPrivateOrderStream(
            credentials,
            proxy_url=proxy_url,
            connect_factory=private_connect_factory,
        )

    def start(self) -> None:
        self.public.start()
        self.private.start()

    def close(self) -> None:
        self.private.close()
        self.public.close()

    def order_book(self, symbol: str, limit: int = 5) -> dict[str, Any]:
        return self.public.order_book(symbol, limit)

    def order_update(self, order_id: str, client_order_id: str) -> Mapping[str, Any] | None:
        return self.private.order_update(order_id, client_order_id)


def _normalized_order_update(row: Mapping[str, Any]) -> dict[str, Any]:
    info = dict(row)
    return {
        "id": str(row.get("id") or ""),
        "clientOrderId": str(row.get("clientOrderId") or ""),
        "side": str(row.get("orderSide") or "").lower(),
        "status": str(row.get("status") or "").lower(),
        "amount": row.get("size") or 0,
        "filled": row.get("cumFillSize") or 0,
        "cost": row.get("cumFillValue") or 0,
        "price": row.get("price") or 0,
        "timeInForce": row.get("timeInForce") or "",
        "postOnly": str(row.get("timeInForce") or "").upper() == "POST_ONLY",
        "info": info,
    }


def _json_payload(raw_message: str | bytes) -> dict[str, Any] | None:
    if isinstance(raw_message, bytes):
        raw_message = raw_message.decode("utf-8")
    payload = json.loads(raw_message)
    return payload if isinstance(payload, dict) else None


def _depth_levels(value: Any) -> list[tuple[Decimal, Decimal]]:
    if not isinstance(value, list):
        return []
    levels: list[tuple[Decimal, Decimal]] = []
    for row in value:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        try:
            price = Decimal(str(row[0]))
            size = Decimal(str(row[1]))
        except (InvalidOperation, TypeError, ValueError):
            continue
        if not price.is_finite() or not size.is_finite() or price <= 0 or size < 0:
            continue
        levels.append((price, size))
    return levels


def _apply_levels(book: dict[Decimal, Decimal], levels: list[tuple[Decimal, Decimal]]) -> None:
    for price, size in levels:
        if size == 0:
            book.pop(price, None)
        else:
            book[price] = size


def _snapshot_update_id(snapshot: Mapping[str, Any]) -> int | None:
    direct = _positive_int(snapshot.get("lastUpdateId")) or _positive_int(snapshot.get("nonce"))
    if direct is not None:
        return direct
    info = snapshot.get("info")
    return _positive_int(info.get("lastUpdateId")) if isinstance(info, Mapping) else None


def _positive_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _retry_delay(consecutive_errors: int) -> float:
    if consecutive_errors <= 0:
        return 0.0
    return min(30.0, float(2 ** min(consecutive_errors - 1, 5)))


def _should_log_reconnect_error(consecutive_errors: int) -> bool:
    """Keep a degraded stream observable without flooding service stderr."""
    return consecutive_errors in {1, 2, 4, 8, 16, 32}


def _socks_proxy_needs_unavailable_dependency(proxy_url: str | None) -> bool:
    if not proxy_url or not proxy_url.lower().startswith("socks"):
        return False
    return importlib.util.find_spec("python_socks") is None


def _websocket_connect(url: str, **kwargs: Any) -> Any:
    from websockets.sync.client import connect

    return connect(url, **kwargs)
