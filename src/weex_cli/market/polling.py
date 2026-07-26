"""REST ticker collection loop with bounded retry and retention maintenance."""

from __future__ import annotations

import math
import sqlite3
import threading
import time
from typing import Any

from weex_cli.core.errors import ValidationError
from weex_cli.core.symbols import live_symbol_id
from weex_cli.presentation.i18n import text

from .contracts import CollectionResult, CollectorStats, MarketGateway, Tick
from .runtime import LOGGER, retry_delay
from .tick_store import TickStore


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
        return CollectionResult(captured_at=captured_at, ticks=ticks, rows_written=rows_written)

    def _fetch_tick(self, symbol: str) -> Tick:
        payload = self.gateway.ticker(symbol)
        try:
            price = float(payload.get("last"))
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"WEEX ticker for {symbol} has no valid last price") from exc
        if not math.isfinite(price) or price <= 0:
            raise ValidationError(f"WEEX ticker for {symbol} has an invalid last price")
        return Tick(symbol=live_symbol_id(symbol), price=price)


def run_market_collector(
    collector: MarketCollector,
    *,
    poll_interval_seconds: float = 1.0,
    cleanup_interval_seconds: float = 300.0,
    log_interval_seconds: float = 60.0,
    once: bool = False,
    stop_event: threading.Event | None = None,
) -> CollectorStats:
    _validate_intervals(poll_interval_seconds, cleanup_interval_seconds, log_interval_seconds)
    stopper = stop_event or threading.Event()
    stats = CollectorStats()
    next_cleanup = next_log = 0.0
    next_poll = time.monotonic()
    while not stopper.is_set():
        try:
            result = collector.collect_once()
        except Exception as exc:  # noqa: BLE001 - network failures must not kill the daemon
            stats.errors += 1
            stats.consecutive_errors += 1
            LOGGER.error(
                text("采集失败 连续错误数=%d 错误=%s", "collection_failed consecutive_errors=%d error=%s"),
                stats.consecutive_errors,
                exc,
            )
        else:
            stats.cycles += 1
            stats.rows_written += result.rows_written
            stats.consecutive_errors = 0
            stats.last_prices = {tick.symbol: tick.price for tick in result.ticks}
        now_monotonic = time.monotonic()
        next_cleanup = _maintenance(collector.store, stats, now_monotonic, next_cleanup, cleanup_interval_seconds)
        if now_monotonic >= next_log:
            _log_status(stats, transport="rest")
            next_log = now_monotonic + log_interval_seconds
        if once:
            break
        next_poll += poll_interval_seconds
        delay = next_poll - time.monotonic()
        if delay <= 0:
            next_poll = time.monotonic()
            delay = min(poll_interval_seconds, retry_delay(stats.consecutive_errors))
        elif stats.consecutive_errors:
            delay = max(delay, retry_delay(stats.consecutive_errors))
            next_poll = time.monotonic() + delay
        stopper.wait(delay)
    return stats


def _validate_intervals(poll: float, cleanup: float, log: float) -> None:
    if poll <= 0:
        raise ValidationError("poll_interval_seconds must be greater than zero")
    if cleanup <= 0:
        raise ValidationError("cleanup_interval_seconds must be greater than zero")
    if log <= 0:
        raise ValidationError("log_interval_seconds must be greater than zero")


def _maintenance(store: TickStore, stats: CollectorStats, now: float, next_at: float, interval: float) -> float:
    if now < next_at:
        return next_at
    try:
        deleted = store.cleanup()
    except sqlite3.Error as exc:
        stats.errors += 1
        LOGGER.error(text("清理失败 错误=%s", "cleanup_failed error=%s"), exc)
    else:
        stats.rows_deleted += deleted
        LOGGER.info(text("清理完成 删除行数=%d", "cleanup_complete rows_deleted=%d"), deleted)
    return now + interval


def _log_status(stats: CollectorStats, *, transport: str) -> None:
    prices = " ".join(f"{symbol}={price}" for symbol, price in sorted(stats.last_prices.items()))
    LOGGER.info(
        text(
            "采集器状态 传输方式=%s 周期数=%d 写入行数=%d 删除行数=%d 错误数=%d 连续错误数=%d %s",
            (
                "collector_status transport=%s cycles=%d rows_written=%d rows_deleted=%d errors=%d "
                "consecutive_errors=%d %s"
            ),
        ),
        transport,
        stats.cycles,
        stats.rows_written,
        stats.rows_deleted,
        stats.errors,
        stats.consecutive_errors,
        prices,
    )
