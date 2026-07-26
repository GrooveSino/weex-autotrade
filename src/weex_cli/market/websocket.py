"""Public WEEX ticker stream collection and bounded reconnection loop."""

from __future__ import annotations

import json
import math
import sqlite3
import threading
import time
from typing import Any

from weex_cli.core.errors import ValidationError
from weex_cli.core.symbols import live_symbol_id
from weex_cli.presentation.i18n import text

from .contracts import (
    DEFAULT_WEBSOCKET_STALE_AFTER_SECONDS,
    WEEX_PUBLIC_WS_URL,
    CollectionResult,
    CollectorStats,
    Tick,
)
from .runtime import LOGGER, retry_delay, websocket_connect
from .tick_store import TickStore


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
        self.connect_factory = connect_factory or websocket_connect
        self.clock = clock
        self.monotonic = monotonic
        self.latest_prices: dict[str, float] = {}
        self.latest_price_at: dict[str, float] = {}
        self.ignored_ticks = 0

    def reset_stream(self) -> None:
        self.latest_prices.clear()
        self.latest_price_at.clear()

    def stale_symbols(self, *, now: float, connected_at: float, stale_after_seconds: float) -> tuple[str, ...]:
        return tuple(
            symbol
            for symbol in self.symbols
            if now - self.latest_price_at.get(symbol, connected_at) >= stale_after_seconds
        )

    def subscription_message(self) -> str:
        return json.dumps(
            {"method": "SUBSCRIBE", "params": [f"{symbol}@ticker" for symbol in self.symbols], "id": 1},
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
        return CollectionResult(captured_at=captured_at, ticks=ticks, rows_written=rows_written)


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
    _validate_intervals(poll_interval_seconds, cleanup_interval_seconds, log_interval_seconds, stale_after_seconds)
    stopper = stop_event or threading.Event()
    stats = CollectorStats()
    next_cleanup = next_log = 0.0

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
                    (
                        "采集器状态 传输方式=websocket 周期数=%d 写入行数=%d 删除行数=%d 错误数=%d "
                        "连续错误数=%d 忽略行情数=%d %s"
                    ),
                    (
                        "collector_status transport=websocket cycles=%d rows_written=%d rows_deleted=%d "
                        "errors=%d consecutive_errors=%d ignored_ticks=%d %s"
                    ),
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
                text("WebSocket 失败 连续错误数=%d 错误=%s", "websocket_failed consecutive_errors=%d error=%s"),
                stats.consecutive_errors,
                exc,
            )
            maintenance()
            if once:
                return stats
            stopper.wait(retry_delay(stats.consecutive_errors))
    return stats


def _validate_intervals(poll: float, cleanup: float, log: float, stale: float) -> None:
    if poll <= 0:
        raise ValidationError("poll_interval_seconds must be greater than zero")
    if cleanup <= 0:
        raise ValidationError("cleanup_interval_seconds must be greater than zero")
    if log <= 0:
        raise ValidationError("log_interval_seconds must be greater than zero")
    if stale <= 0:
        raise ValidationError("stale_after_seconds must be greater than zero")
