"""One credential-free public BTC/ETH market snapshot for Fleet actors."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from weex_cli.control_api.exchange import Credentials, Settings, WeexGateway
from weex_cli.control_api.streams import WeexPublicOrderBookStream

_REST_FALLBACK_INTERVAL_SECONDS = 0.5


class PublicOrderBook(Protocol):
    @property
    def connected(self) -> bool: ...

    def start(self) -> None: ...

    def close(self) -> None: ...

    def order_book(self, symbol: str, limit: int = 5) -> dict[str, Any]: ...


class SharedMarketUnavailable(RuntimeError):
    """The shared reader is unavailable; never trigger an account REST fallback."""


@dataclass(frozen=True, slots=True)
class PublicMarketSnapshot:
    enabled: bool
    connected: bool
    generation: int
    btc_snapshot_age_ms: int | None
    eth_snapshot_age_ms: int | None
    rest_fallback_count: int
    reconnect_count: int
    waiting_phase_count: int
    source_state: str

    @property
    def active_leases(self) -> int:
        """Compatibility metric: every actor reads one shared, lease-free book."""
        return 0

    @property
    def shared_connections(self) -> int:
        return int(self.enabled and self.connected)

    @property
    def idle_connections(self) -> int:
        return 0


class _CountingGateway:
    def __init__(self, gateway: WeexGateway, on_rest_snapshot: Callable[[], None]) -> None:
        self._gateway = gateway
        self._on_rest_snapshot = on_rest_snapshot

    def order_book(self, symbol: str, limit: int = 10) -> dict[str, Any]:
        self._on_rest_snapshot()
        return self._gateway.order_book(symbol, limit)

    def close(self) -> None:
        self._gateway.close()


class _SharedOrderBookView:
    def __init__(
        self,
        service: PublicMarketSnapshotService,
        stop: threading.Event,
        max_wait_seconds: float | None,
    ) -> None:
        self._service = service
        self._stop = stop
        self._max_wait_seconds = max_wait_seconds

    def order_book(self, symbol: str, limit: int = 5) -> dict[str, Any]:
        return self._service.order_book(symbol, limit, stop=self._stop, max_wait_seconds=self._max_wait_seconds)


class PublicMarketSnapshotService:
    """Maintain one direct public stream without credentials or account proxies."""

    def __init__(
        self,
        *,
        enabled: bool,
        request_timeout_ms: int,
        proxy_url: str | None = None,
        gateway_factory: Callable[[str | None], WeexGateway] | None = None,
        stream_factory: Callable[[Any, str | None], PublicOrderBook] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._enabled = enabled
        self._request_timeout_ms = request_timeout_ms
        self._proxy_url = proxy_url or None
        self._gateway_factory = gateway_factory or self._new_public_gateway
        self._stream_factory = stream_factory or self._new_stream
        self._monotonic = monotonic
        self._lock = threading.Condition()
        self._stop = threading.Event()
        self._gateway: _CountingGateway | None = None
        self._stream: PublicOrderBook | None = None
        self._thread: threading.Thread | None = None
        self._books: dict[str, tuple[dict[str, Any], float]] = {}
        self._waiting: set[str] = set()
        self._connected = False
        self._generation = 0
        self._reconnect_count = 0
        self._rest_fallback_count = 0
        self._source_state = "disabled" if not enabled else "starting"

    def start(self) -> None:
        if not self._enabled:
            return
        with self._lock:
            if self._thread is not None:
                return
            gateway = self._gateway_factory(self._proxy_url)
            self._gateway = _CountingGateway(gateway, self._count_rest_snapshot)
            self._stream = self._stream_factory(self._gateway, self._proxy_url)
            self._stream.start()
            self._thread = threading.Thread(target=self._observe, name="fleet-public-market", daemon=True)
            self._thread.start()

    @property
    def enabled(self) -> bool:
        return self._enabled

    def close(self) -> None:
        self._stop.set()
        with self._lock:
            self._lock.notify_all()
            stream, gateway, thread = self._stream, self._gateway, self._thread
            self._stream = None
            self._gateway = None
            self._thread = None
            self._books.clear()
            self._connected = False
            self._source_state = "stopped" if self._enabled else "disabled"
        if stream is not None:
            stream.close()
        if gateway is not None:
            gateway.close()
        if thread is not None:
            thread.join(timeout=3)

    def fresh(self) -> bool:
        with self._lock:
            return self._fresh_locked()

    def actor_view(
        self,
        stop: threading.Event,
        *,
        max_wait_seconds: float | None = None,
    ) -> _SharedOrderBookView:
        return _SharedOrderBookView(self, stop, max_wait_seconds)

    def set_waiting(self, execution_id: str, waiting: bool) -> None:
        with self._lock:
            if waiting:
                self._waiting.add(execution_id)
            else:
                self._waiting.discard(execution_id)
            self._lock.notify_all()

    def order_book(
        self,
        symbol: str,
        limit: int,
        *,
        stop: threading.Event,
        max_wait_seconds: float | None,
    ) -> dict[str, Any]:
        target = symbol.upper()
        deadline = None if max_wait_seconds is None else self._monotonic() + max_wait_seconds
        while not stop.is_set():
            with self._lock:
                item = self._books.get(target)
                if self._fresh_locked() and item is not None:
                    return _limit_book(
                        item[0],
                        limit,
                        source="shared_websocket" if self._connected else "shared_rest_fallback",
                    )
                if deadline is not None and self._monotonic() >= deadline:
                    break
                self._lock.wait(timeout=0.1)
        raise SharedMarketUnavailable("共享行情在允许等待时间内未恢复")

    def snapshot(self) -> PublicMarketSnapshot:
        with self._lock:
            return PublicMarketSnapshot(
                enabled=self._enabled,
                connected=self._connected,
                generation=self._generation,
                btc_snapshot_age_ms=self._age_ms_locked("BTC"),
                eth_snapshot_age_ms=self._age_ms_locked("ETH"),
                rest_fallback_count=self._rest_fallback_count,
                reconnect_count=self._reconnect_count,
                waiting_phase_count=len(self._waiting),
                source_state=self._source_state,
            )

    def _observe(self) -> None:
        previously_connected = False
        last_rest_fallback_at = float("-inf")
        while not self._stop.wait(0.1):
            stream = self._stream
            connected = bool(stream is not None and stream.connected)
            with self._lock:
                if connected and not previously_connected:
                    self._generation += 1
                    if self._generation > 1:
                        self._reconnect_count += 1
                self._connected = connected
            previously_connected = connected
            if stream is not None and connected:
                for symbol in ("BTC", "ETH"):
                    try:
                        book = stream.order_book(symbol, 15)
                    except Exception:
                        continue
                    with self._lock:
                        self._books[symbol] = (book, self._monotonic())
            elif self._monotonic() - last_rest_fallback_at >= _REST_FALLBACK_INTERVAL_SECONDS:
                last_rest_fallback_at = self._monotonic()
                self._refresh_rest_fallback()
            with self._lock:
                self._source_state = _source_state(self._fresh_locked(), connected)
                self._lock.notify_all()

    def _refresh_rest_fallback(self) -> None:
        """Refresh one shared public REST snapshot while the stream reconnects."""
        gateway = self._gateway
        if gateway is None:
            return
        try:
            books = {symbol: gateway.order_book(symbol, 15) for symbol in ("BTC", "ETH")}
            if not all(_book_has_spread(book) for book in books.values()):
                return
        except Exception:
            return
        received_at = self._monotonic()
        with self._lock:
            for symbol, book in books.items():
                self._books[symbol] = (book, received_at)

    def _fresh_locked(self) -> bool:
        if not self._enabled:
            return False
        return all(_snapshot_is_fresh(self._age_ms_locked(symbol)) for symbol in ("BTC", "ETH"))

    def _age_ms_locked(self, symbol: str) -> int | None:
        item = self._books.get(symbol)
        if item is None:
            return None
        age = int(max(0, self._monotonic() - item[1]) * 1_000)
        return age

    def _count_rest_snapshot(self) -> None:
        with self._lock:
            self._rest_fallback_count += 1

    def _new_public_gateway(self, proxy_url: str | None) -> WeexGateway:
        settings = Settings(
            credentials=Credentials(api_key="", api_secret="", passphrase=""),
            default_mode="live",
            live_trading_enabled=False,
            timeout_ms=self._request_timeout_ms,
            enable_rate_limit=True,
        )
        return WeexGateway(settings, proxy_url=proxy_url)

    @staticmethod
    def _new_stream(gateway: Any, proxy_url: str | None) -> PublicOrderBook:
        return WeexPublicOrderBookStream(gateway, proxy_url=proxy_url, max_age_seconds=1.0)


def _limit_book(book: dict[str, Any], limit: int, *, source: str) -> dict[str, Any]:
    return {
        **book,
        "bids": list(book.get("bids") or [])[:limit],
        "asks": list(book.get("asks") or [])[:limit],
        "source": source,
    }


def _source_state(fresh: bool, connected: bool) -> str:
    if fresh:
        return "realtime" if connected else "rest_fallback"
    return "recovering" if connected else "disconnected"


def _snapshot_is_fresh(age_ms: int | None) -> bool:
    return age_ms is not None and age_ms <= 1_000


def _book_has_spread(book: dict[str, Any]) -> bool:
    bids = book.get("bids") or []
    asks = book.get("asks") or []
    return bool(bids and asks)
