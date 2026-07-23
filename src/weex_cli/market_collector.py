from __future__ import annotations

import json
import logging
import math
import signal
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import FrameType
from typing import Any, Protocol

from weex_cli.errors import ValidationError
from weex_cli.i18n import text
from weex_cli.symbols import live_symbol_id

LOGGER = logging.getLogger(__name__)
CHINA_STANDARD_TIME = timezone(timedelta(hours=8))
WEEX_PUBLIC_WS_URL = "wss://ws-contract.weex.com/v3/ws/public"
DEFAULT_WEBSOCKET_STALE_AFTER_SECONDS = 30.0


class MarketGateway(Protocol):
    def ticker(self, symbol: str) -> dict[str, Any]: ...


@dataclass(frozen=True)
class Tick:
    symbol: str
    price: float


@dataclass(frozen=True)
class CollectionResult:
    captured_at: float
    ticks: tuple[Tick, ...]
    rows_written: int


@dataclass
class CollectorStats:
    cycles: int = 0
    rows_written: int = 0
    rows_deleted: int = 0
    errors: int = 0
    consecutive_errors: int = 0
    ignored_ticks: int = 0
    last_prices: dict[str, float] = field(default_factory=dict)


class TickStore:
    """SQLite writer compatible with the weex-calc ticks table."""

    def __init__(self, db_path: Path, *, retention_hours: float = 12.0) -> None:
        if retention_hours <= 0:
            raise ValidationError("retention_hours must be greater than zero")
        self.db_path = db_path.expanduser().resolve()
        self.retention_seconds = retention_hours * 3600
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.db_path, timeout=5.0)
        self.connection.execute("PRAGMA busy_timeout=5000")
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=NORMAL")
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with self.connection:
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS ticks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    price REAL NOT NULL,
                    timestamp REAL NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(symbol, timestamp)
                )
                """
            )
            self.connection.execute("CREATE INDEX IF NOT EXISTS idx_ticks_symbol_ts ON ticks(symbol, timestamp)")
            self.connection.execute("CREATE INDEX IF NOT EXISTS idx_ticks_timestamp ON ticks(timestamp)")

    def write(self, ticks: tuple[Tick, ...], *, captured_at: float) -> int:
        if not ticks:
            return 0
        created_at = datetime.fromtimestamp(captured_at, tz=CHINA_STANDARD_TIME).isoformat(timespec="milliseconds")
        before = self.connection.total_changes
        with self.connection:
            self.connection.executemany(
                """
                INSERT OR IGNORE INTO ticks (symbol, price, timestamp, created_at)
                VALUES (?, ?, ?, ?)
                """,
                ((tick.symbol, tick.price, captured_at, created_at) for tick in ticks),
            )
        return self.connection.total_changes - before

    def cleanup(self, *, now: float | None = None) -> int:
        cutoff = (time.time() if now is None else now) - self.retention_seconds
        with self.connection:
            cursor = self.connection.execute("DELETE FROM ticks WHERE timestamp < ?", (cutoff,))
        return max(0, cursor.rowcount)

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> TickStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class MarketCollector:
    def __init__(
        self,
        gateway: MarketGateway,
        store: TickStore,
        symbols: tuple[str, ...],
        *,
        clock: Any = time.time,
    ) -> None:
        if not symbols:
            raise ValidationError("at least one symbol is required")
        self.gateway = gateway
        self.store = store
        self.symbols = symbols
        self.clock = clock

    def collect_once(self) -> CollectionResult:
        ticks = tuple(self._fetch_tick(symbol) for symbol in self.symbols)
        captured_at = float(self.clock())
        rows_written = self.store.write(ticks, captured_at=captured_at)
        return CollectionResult(
            captured_at=captured_at,
            ticks=ticks,
            rows_written=rows_written,
        )

    def _fetch_tick(self, symbol: str) -> Tick:
        payload = self.gateway.ticker(symbol)
        try:
            price = float(payload.get("last"))
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"WEEX ticker for {symbol} has no valid last price") from exc
        if not math.isfinite(price) or price <= 0:
            raise ValidationError(f"WEEX ticker for {symbol} has an invalid last price")
        return Tick(symbol=live_symbol_id(symbol), price=price)


class WebSocketMarketCollector:
    """Consume the public WEEX ticker stream and persist aligned snapshots."""

    def __init__(
        self,
        store: TickStore,
        symbols: tuple[str, ...],
        *,
        url: str = WEEX_PUBLIC_WS_URL,
        connect_factory: Any | None = None,
        clock: Any = time.time,
        monotonic: Any = time.monotonic,
    ) -> None:
        if not symbols:
            raise ValidationError("at least one symbol is required")
        self.store = store
        self.symbols = tuple(live_symbol_id(symbol) for symbol in symbols)
        self.url = url
        self.connect_factory = connect_factory or _websocket_connect
        self.clock = clock
        self.monotonic = monotonic
        self.latest_prices: dict[str, float] = {}
        self.latest_price_at: dict[str, float] = {}
        self.ignored_ticks = 0

    def reset_stream(self) -> None:
        self.latest_prices.clear()
        self.latest_price_at.clear()

    def stale_symbols(
        self,
        *,
        now: float,
        connected_at: float,
        stale_after_seconds: float,
    ) -> tuple[str, ...]:
        return tuple(
            symbol
            for symbol in self.symbols
            if now - self.latest_price_at.get(symbol, connected_at) >= stale_after_seconds
        )

    def subscription_message(self) -> str:
        return json.dumps(
            {
                "method": "SUBSCRIBE",
                "params": [f"{symbol}@ticker" for symbol in self.symbols],
                "id": 1,
            },
            separators=(",", ":"),
        )

    def handle_message(self, websocket: Any, raw_message: str | bytes) -> None:
        if isinstance(raw_message, bytes):
            raw_message = raw_message.decode("utf-8")
        payload = json.loads(raw_message)
        if not isinstance(payload, dict):
            return
        if payload.get("event") == "ping":
            websocket.send('{"method":"PONG","id":1}')
            return
        if "result" in payload:
            if payload.get("result") is not True:
                raise ValidationError(f"WEEX ticker subscription failed: {payload.get('msg') or 'unknown error'}")
            return
        if payload.get("e") != "ticker":
            return

        symbol = str(payload.get("s") or "").upper()
        rows = payload.get("d")
        if symbol not in self.symbols or not isinstance(rows, list) or not rows:
            return
        row = rows[0]
        if not isinstance(row, dict):
            return
        try:
            price = float(row.get("c"))
        except (TypeError, ValueError):
            self.ignored_ticks += 1
            return
        if not math.isfinite(price) or price <= 0:
            self.ignored_ticks += 1
            return
        self.latest_prices[symbol] = price
        self.latest_price_at[symbol] = float(self.monotonic())

    def snapshot(self) -> CollectionResult | None:
        if any(symbol not in self.latest_prices for symbol in self.symbols):
            return None
        captured_at = float(self.clock())
        ticks = tuple(Tick(symbol=symbol, price=self.latest_prices[symbol]) for symbol in self.symbols)
        rows_written = self.store.write(ticks, captured_at=captured_at)
        return CollectionResult(
            captured_at=captured_at,
            ticks=ticks,
            rows_written=rows_written,
        )


def run_market_collector(
    collector: MarketCollector,
    *,
    poll_interval_seconds: float = 1.0,
    cleanup_interval_seconds: float = 300.0,
    log_interval_seconds: float = 60.0,
    once: bool = False,
    stop_event: threading.Event | None = None,
) -> CollectorStats:
    if poll_interval_seconds <= 0:
        raise ValidationError("poll_interval_seconds must be greater than zero")
    if cleanup_interval_seconds <= 0:
        raise ValidationError("cleanup_interval_seconds must be greater than zero")
    if log_interval_seconds <= 0:
        raise ValidationError("log_interval_seconds must be greater than zero")

    stopper = stop_event or threading.Event()
    stats = CollectorStats()
    next_cleanup = 0.0
    next_log = 0.0
    next_poll = time.monotonic()

    while not stopper.is_set():
        try:
            result = collector.collect_once()
        except Exception as exc:  # noqa: BLE001 - network failures must not kill the daemon
            stats.errors += 1
            stats.consecutive_errors += 1
            LOGGER.error(
                text(
                    "采集失败 连续错误数=%d 错误=%s",
                    "collection_failed consecutive_errors=%d error=%s",
                ),
                stats.consecutive_errors,
                exc,
            )
        else:
            stats.cycles += 1
            stats.rows_written += result.rows_written
            stats.consecutive_errors = 0
            stats.last_prices = {tick.symbol: tick.price for tick in result.ticks}

        now_monotonic = time.monotonic()
        if now_monotonic >= next_cleanup:
            try:
                deleted = collector.store.cleanup()
            except sqlite3.Error as exc:
                stats.errors += 1
                LOGGER.error(text("清理失败 错误=%s", "cleanup_failed error=%s"), exc)
            else:
                stats.rows_deleted += deleted
                LOGGER.info(text("清理完成 删除行数=%d", "cleanup_complete rows_deleted=%d"), deleted)
            next_cleanup = now_monotonic + cleanup_interval_seconds

        if now_monotonic >= next_log:
            prices = " ".join(f"{symbol}={price}" for symbol, price in sorted(stats.last_prices.items()))
            LOGGER.info(
                text(
                    "采集器状态 周期数=%d 写入行数=%d 删除行数=%d 错误数=%d 连续错误数=%d %s",
                    "collector_status cycles=%d rows_written=%d rows_deleted=%d errors=%d consecutive_errors=%d %s",
                ),
                stats.cycles,
                stats.rows_written,
                stats.rows_deleted,
                stats.errors,
                stats.consecutive_errors,
                prices,
            )
            next_log = now_monotonic + log_interval_seconds

        if once:
            break

        next_poll += poll_interval_seconds
        delay = next_poll - time.monotonic()
        if delay <= 0:
            next_poll = time.monotonic()
            delay = min(poll_interval_seconds, _retry_delay(stats.consecutive_errors))
        elif stats.consecutive_errors:
            delay = max(delay, _retry_delay(stats.consecutive_errors))
            next_poll = time.monotonic() + delay
        stopper.wait(delay)

    return stats


def run_websocket_market_collector(
    collector: WebSocketMarketCollector,
    *,
    poll_interval_seconds: float = 1.0,
    cleanup_interval_seconds: float = 300.0,
    log_interval_seconds: float = 60.0,
    stale_after_seconds: float = DEFAULT_WEBSOCKET_STALE_AFTER_SECONDS,
    once: bool = False,
    stop_event: threading.Event | None = None,
) -> CollectorStats:
    if poll_interval_seconds <= 0:
        raise ValidationError("poll_interval_seconds must be greater than zero")
    if cleanup_interval_seconds <= 0:
        raise ValidationError("cleanup_interval_seconds must be greater than zero")
    if log_interval_seconds <= 0:
        raise ValidationError("log_interval_seconds must be greater than zero")
    if stale_after_seconds <= 0:
        raise ValidationError("stale_after_seconds must be greater than zero")

    stopper = stop_event or threading.Event()
    stats = CollectorStats()
    next_cleanup = 0.0
    next_log = 0.0

    def maintenance() -> None:
        nonlocal next_cleanup, next_log
        now_monotonic = collector.monotonic()
        if now_monotonic >= next_cleanup:
            try:
                deleted = collector.store.cleanup()
            except sqlite3.Error as exc:
                stats.errors += 1
                LOGGER.error(text("清理失败 错误=%s", "cleanup_failed error=%s"), exc)
            else:
                stats.rows_deleted += deleted
                LOGGER.info(text("清理完成 删除行数=%d", "cleanup_complete rows_deleted=%d"), deleted)
            next_cleanup = now_monotonic + cleanup_interval_seconds
        if now_monotonic >= next_log:
            stats.ignored_ticks = collector.ignored_ticks
            prices = " ".join(f"{symbol}={price}" for symbol, price in sorted(stats.last_prices.items()))
            LOGGER.info(
                text(
                    "采集器状态 传输方式=websocket 周期数=%d 写入行数=%d 删除行数=%d "
                    "错误数=%d 连续错误数=%d 忽略行情数=%d %s",
                    "collector_status transport=websocket cycles=%d rows_written=%d "
                    "rows_deleted=%d errors=%d consecutive_errors=%d ignored_ticks=%d %s",
                ),
                stats.cycles,
                stats.rows_written,
                stats.rows_deleted,
                stats.errors,
                stats.consecutive_errors,
                stats.ignored_ticks,
                prices,
            )
            next_log = now_monotonic + log_interval_seconds

    while not stopper.is_set():
        try:
            with collector.connect_factory(
                collector.url,
                open_timeout=15,
                close_timeout=5,
                ping_interval=None,
                proxy=None,
            ) as websocket:
                websocket.send(collector.subscription_message())
                LOGGER.info(
                    text("WebSocket 已连接 交易对=%s", "websocket_connected symbols=%s"),
                    ",".join(collector.symbols),
                )
                collector.reset_stream()
                connected_at = collector.monotonic()
                next_sample: float | None = None
                while not stopper.is_set():
                    now_monotonic = collector.monotonic()
                    timeout = 1.0 if next_sample is None else max(0.01, min(1.0, next_sample - now_monotonic))
                    try:
                        message = websocket.recv(timeout=timeout)
                    except TimeoutError:
                        message = None
                    if message is not None:
                        collector.handle_message(websocket, message)

                    now_monotonic = collector.monotonic()
                    stale_symbols = collector.stale_symbols(
                        now=now_monotonic,
                        connected_at=connected_at,
                        stale_after_seconds=stale_after_seconds,
                    )
                    if stale_symbols:
                        raise ValidationError("WEEX ticker stream stale for " + ",".join(stale_symbols))
                    ready = all(symbol in collector.latest_prices for symbol in collector.symbols)
                    if ready and (next_sample is None or now_monotonic >= next_sample):
                        result = collector.snapshot()
                        if result is not None:
                            stats.cycles += 1
                            stats.rows_written += result.rows_written
                            stats.consecutive_errors = 0
                            stats.last_prices = {tick.symbol: tick.price for tick in result.ticks}
                        if once:
                            maintenance()
                            return stats
                        next_sample = now_monotonic + poll_interval_seconds
                    maintenance()
        except Exception as exc:  # noqa: BLE001 - reconnect after transport/protocol failures
            stats.errors += 1
            stats.consecutive_errors += 1
            LOGGER.error(
                text(
                    "WebSocket 失败 连续错误数=%d 错误=%s",
                    "websocket_failed consecutive_errors=%d error=%s",
                ),
                stats.consecutive_errors,
                exc,
            )
            maintenance()
            if once:
                return stats
            stopper.wait(_retry_delay(stats.consecutive_errors))

    return stats


def install_stop_handlers(stop_event: threading.Event) -> None:
    def request_stop(signum: int, _frame: FrameType | None) -> None:
        LOGGER.info(text("收到停止请求 信号=%d", "stop_requested signal=%d"), signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)


def _retry_delay(consecutive_errors: int) -> float:
    if consecutive_errors <= 0:
        return 0.0
    return min(60.0, float(2 ** min(consecutive_errors - 1, 6)))


def _websocket_connect(url: str, **kwargs: Any) -> Any:
    from websockets.sync.client import connect

    return connect(url, **kwargs)
