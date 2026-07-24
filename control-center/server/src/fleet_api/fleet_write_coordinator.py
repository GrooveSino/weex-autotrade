"""Serialized Fleet journal writes with bounded low-priority coalescing."""

from __future__ import annotations

import queue
import time
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
from threading import Lock, Thread
from typing import Any, TypeVar

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class FleetWriteSnapshot:
    queued_critical: int
    queued_low_priority: int
    committed: int
    failed: int
    p95_latency_ms: int


@dataclass(slots=True)
class _Write:
    callback: Callable[[], Any]
    future: Future[Any]
    queued_at: float


class FleetWriteCoordinator:
    """One local writer ordering durable mutations before observer notification.

    Critical callers wait for their journal transaction.  Heartbeats may share
    a key and are flushed at most every ``low_priority_window_ms``; a fresh
    heartbeat supersedes an older pending one without creating a second write.
    """

    def __init__(self, *, low_priority_window_ms: int = 100, low_priority_batch: int = 20) -> None:
        self._window = low_priority_window_ms / 1_000
        self._batch = low_priority_batch
        self._critical: queue.Queue[_Write | None] = queue.Queue()
        self._low: dict[str, _Write] = {}
        self._lock = Lock()
        self._closed = False
        self._committed = 0
        self._failed = 0
        self._latencies: list[int] = []
        self._last_low_flush_at = time.monotonic()
        self._thread = Thread(target=self._run, name="fleet-writer", daemon=True)
        self._thread.start()

    def critical(self, callback: Callable[[], T]) -> T:
        write: _Write = _Write(callback, Future(), time.monotonic())
        with self._lock:
            if self._closed:
                raise RuntimeError("fleet write coordinator is closed")
            self._queue_low_locked()
            self._critical.put(write)
        return write.future.result()

    def low_priority(self, key: str, callback: Callable[[], T]) -> Future[T]:
        write: _Write = _Write(callback, Future(), time.monotonic())
        with self._lock:
            if self._closed:
                raise RuntimeError("fleet write coordinator is closed")
            existing = self._low.get(key)
            if existing is not None and not existing.future.done():
                existing.future.set_result(None)
            self._low[key] = write
        return write.future

    def snapshot(self) -> FleetWriteSnapshot:
        with self._lock:
            latencies = sorted(self._latencies)
            p95 = latencies[int((len(latencies) - 1) * 0.95)] if latencies else 0
            return FleetWriteSnapshot(
                queued_critical=self._critical.qsize(),
                queued_low_priority=len(self._low),
                committed=self._committed,
                failed=self._failed,
                p95_latency_ms=p95,
            )

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._critical.put(None)
        self._thread.join(timeout=10)

    def _run(self) -> None:
        while True:
            try:
                write = self._critical.get(timeout=self._window)
            except queue.Empty:
                self._flush_low()
                continue
            if write is None:
                self._flush_all_low()
                return
            self._commit(write)
            if self._low_flush_due():
                self._flush_low()

    def _low_flush_due(self) -> bool:
        with self._lock:
            return len(self._low) >= self._batch or time.monotonic() - self._last_low_flush_at >= self._window

    def _flush_low(self) -> None:
        with self._lock:
            writes = list(self._low.values())[: self._batch]
            for key, value in tuple(self._low.items()):
                if value in writes:
                    self._low.pop(key)
            if writes:
                self._last_low_flush_at = time.monotonic()
        for write in writes:
            self._commit(write)

    def _queue_low_locked(self) -> None:
        for write in self._low.values():
            self._critical.put(write)
        self._low.clear()

    def _flush_all_low(self) -> None:
        while True:
            with self._lock:
                if not self._low:
                    return
            self._flush_low()

    def _commit(self, write: _Write) -> None:
        try:
            result = write.callback()
        except Exception as exc:  # The caller receives the exact durable failure.
            with self._lock:
                self._failed += 1
            write.future.set_exception(exc)
        else:
            with self._lock:
                self._committed += 1
            write.future.set_result(result)
        finally:
            latency = int((time.monotonic() - write.queued_at) * 1_000)
            with self._lock:
                self._latencies = (self._latencies + [latency])[-200:]
