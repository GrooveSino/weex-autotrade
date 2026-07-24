"""Bounded Fleet execution admission and normal-phase scheduling."""

from __future__ import annotations

import hashlib
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from threading import Condition, Event


@dataclass(frozen=True, slots=True)
class ExecutionCapacitySnapshot:
    active_executions: int
    max_active_executions: int
    active_normal_phases: int
    max_normal_phases: int
    queued_normal_phases: int
    phase_start_rate_per_second: float
    per_proxy_gap_seconds: float
    active_proxy_partitions: int
    queued_proxy_limited_phases: int
    phase_queue_p50_ms: int
    phase_queue_p95_ms: int
    revision: int


@dataclass(frozen=True, slots=True)
class PhaseQueueState:
    key: str
    queue_position: int
    queued_at_ms: int
    estimated_start_at_ms: int


@dataclass(frozen=True, slots=True)
class PhaseReservation:
    key: str
    queue_position: int
    estimated_start_at_ms: int


@dataclass(frozen=True, slots=True)
class _QueuedPhase:
    key: str
    proxy_key: str
    queued_at: float
    queued_at_ms: int
    not_before: float


class ExecutionCapacity:
    """Single-process capacity gate for logical tasks and normal order phases.

    The queue is fair by arrival order but is deliberately not head-of-line
    blocking: a phase waiting on its proxy's cooldown cannot stall another
    proxy that is already eligible for the next global start budget.
    """

    def __init__(
        self,
        *,
        max_active_executions: int,
        max_normal_phases: int,
        phase_start_rate_per_second: float,
        per_proxy_gap_seconds: float,
        stable_jitter_seconds: float = 0,
        now: Callable[[], float] = time.monotonic,
        now_ms: Callable[[], int] = lambda: time.time_ns() // 1_000_000,
    ) -> None:
        if max_active_executions < 1 or max_normal_phases < 1:
            raise ValueError("execution capacity limits must be positive")
        if phase_start_rate_per_second <= 0 or per_proxy_gap_seconds < 0 or stable_jitter_seconds < 0:
            raise ValueError("execution phase timings are invalid")
        self._max_active = max_active_executions
        self._max_normal = max_normal_phases
        self._phase_interval = 1 / phase_start_rate_per_second
        self._per_proxy_gap = per_proxy_gap_seconds
        self._stable_jitter_seconds = stable_jitter_seconds
        self._now = now
        self._now_ms = now_ms
        self._condition = Condition()
        self._admitted: set[str] = set()
        self._active: dict[str, str] = {}
        self._queued: deque[_QueuedPhase] = deque()
        self._next_global_at = 0.0
        self._next_proxy_at: dict[str, float] = {}
        self._phase_queue_wait_ms: deque[int] = deque(maxlen=200)
        self._revision = 0

    def admit(self, execution_id: str) -> bool:
        with self._condition:
            if execution_id in self._admitted:
                return True
            if len(self._admitted) >= self._max_active:
                return False
            self._admitted.add(execution_id)
            self._bump()
            return True

    def release_execution(self, execution_id: str) -> None:
        with self._condition:
            changed = execution_id in self._admitted
            self._admitted.discard(execution_id)
            active = [key for key in self._active if key.startswith(f"{execution_id}:")]
            for key in active:
                self._active.pop(key, None)
                changed = True
            queued = [item for item in self._queued if item.key.startswith(f"{execution_id}:")]
            for item in queued:
                self._queued.remove(item)
                changed = True
            if changed:
                self._bump()

    def enqueue_phase(self, key: str, *, proxy_key: str) -> PhaseQueueState:
        if not key or not proxy_key:
            raise ValueError("phase key and proxy key are required")
        with self._condition:
            item = self._queued_item(key)
            if item is None:
                current = self._now()
                item = _QueuedPhase(
                    key=key,
                    proxy_key=proxy_key,
                    queued_at=current,
                    queued_at_ms=self._now_ms(),
                    not_before=current + self._stable_jitter(key),
                )
                self._queued.append(item)
                self._bump()
            return self._queue_state(item)

    def wait_for_phase(
        self,
        key: str,
        *,
        proxy_key: str,
        stop_event: Event,
    ) -> PhaseReservation | None:
        with self._condition:
            if key in self._active:
                return PhaseReservation(key, 0, self._now_ms())
            self.enqueue_phase(key, proxy_key=proxy_key)
            while True:
                if stop_event.is_set():
                    self._remove_queued(key)
                    return None
                current = self._now()
                item = self._queued_item(key)
                if item is None:
                    return None
                selected = self._next_eligible(current)
                if selected is item and len(self._active) < self._max_normal and current >= self._next_global_at:
                    return self._start_phase(item, current)
                self._condition.wait(timeout=min(0.25, self._next_wake_delay(current)))

    def try_start_phase(self, key: str, *, proxy_key: str) -> PhaseReservation | None:
        """Acquire a normal phase without blocking an actor or event-loop thread.

        Async actors use this method with their own cancellable timer.  The
        legacy blocking worker continues to use :meth:`wait_for_phase`.
        """
        with self._condition:
            if key in self._active:
                return PhaseReservation(key, 0, self._now_ms())
            state = self.enqueue_phase(key, proxy_key=proxy_key)
            item = self._queued_item(key)
            if item is None:
                return None
            current = self._now()
            if (
                self._next_eligible(current) is not item
                or len(self._active) >= self._max_normal
                or current < self._next_global_at
            ):
                return None
            return self._start_phase(item, current, queue_position=state.queue_position)

    def cancel_phase(self, key: str) -> bool:
        """Remove a not-yet-started phase and report whether queue state changed."""
        with self._condition:
            before = len(self._queued)
            self._remove_queued(key)
            return len(self._queued) != before

    def next_phase_wake_seconds(self, key: str) -> float:
        """Return a bounded delay for nonblocking queue polling."""
        with self._condition:
            if key in self._active:
                return 0.0
            item = self._queued_item(key)
            if item is None:
                return 0.05
            current = self._now()
            if self._next_eligible(current) is item:
                return 0.0
            # Actor queue state itself is throttled before it reaches the
            # journal. Wake at the next short global-budget window instead of
            # forcing every phase through a 50ms serial gate when the configured
            # start budget is intentionally higher than 20 phases per second.
            return min(0.05, self._next_wake_delay(current))

    def finish_phase(self, key: str) -> None:
        with self._condition:
            if self._active.pop(key, None) is not None:
                self._bump()

    def snapshot(self) -> ExecutionCapacitySnapshot:
        with self._condition:
            waits = sorted(self._phase_queue_wait_ms)
            current = self._now()
            active_proxies = set(self._active.values())
            proxy_limited = sum(
                item.proxy_key in active_proxies or current < self._next_proxy_at.get(item.proxy_key, 0.0)
                for item in self._queued
            )
            return ExecutionCapacitySnapshot(
                active_executions=len(self._admitted),
                max_active_executions=self._max_active,
                active_normal_phases=len(self._active),
                max_normal_phases=self._max_normal,
                queued_normal_phases=len(self._queued),
                phase_start_rate_per_second=1 / self._phase_interval,
                per_proxy_gap_seconds=self._per_proxy_gap,
                active_proxy_partitions=len(set(self._active.values())),
                queued_proxy_limited_phases=proxy_limited,
                phase_queue_p50_ms=self._percentile(waits, 0.5),
                phase_queue_p95_ms=self._percentile(waits, 0.95),
                revision=self._revision,
            )

    def queue_state(self, key: str) -> PhaseQueueState | None:
        with self._condition:
            item = self._queued_item(key)
            return self._queue_state(item) if item is not None else None

    def queue_constraint(self, key: str) -> str:
        """Return the dominant visible reason a queued phase cannot start.

        This is deliberately an abstract resource reason.  It never exposes a
        proxy URL or account identity, but lets the monitor distinguish normal
        global pacing from a same-proxy cooldown or occupied proxy partition.
        """
        with self._condition:
            item = self._queued_item(key)
            if item is None:
                return "queued"
            current = self._now()
            if item.proxy_key in self._active.values():
                return "proxy_active"
            if current < self._next_proxy_at.get(item.proxy_key, 0.0):
                return "proxy_cooldown"
            if current < item.not_before:
                return "stable_jitter"
            if len(self._active) >= self._max_normal:
                return "phase_capacity"
            if current < self._next_global_at:
                return "global_rate"
            return "queue_order"

    def _next_eligible(self, current: float) -> _QueuedPhase | None:
        if len(self._active) >= self._max_normal or current < self._next_global_at:
            return None
        for item in self._queued:
            proxy_ready = self._next_proxy_at.get(item.proxy_key, 0.0)
            if item.proxy_key not in self._active.values() and current >= max(item.not_before, proxy_ready):
                return item
        return None

    def _start_phase(
        self,
        item: _QueuedPhase,
        current: float,
        *,
        queue_position: int | None = None,
    ) -> PhaseReservation:
        position = queue_position if queue_position is not None else self._queue_position(item.key)
        self._queued.remove(item)
        self._active[item.key] = item.proxy_key
        self._next_global_at = current + self._phase_interval
        self._next_proxy_at[item.proxy_key] = current + self._per_proxy_gap
        self._phase_queue_wait_ms.append(max(0, int((current - item.queued_at) * 1_000)))
        self._bump()
        return PhaseReservation(item.key, position, self._now_ms())

    def _queue_state(self, item: _QueuedPhase) -> PhaseQueueState:
        position = self._queue_position(item.key)
        current = self._now()
        proxy_ready = self._next_proxy_at.get(item.proxy_key, 0.0)
        earliest = max(current, item.not_before, proxy_ready, self._next_global_at)
        estimated = earliest + max(0, position - 1) * self._phase_interval
        return PhaseQueueState(
            key=item.key,
            queue_position=position,
            queued_at_ms=item.queued_at_ms,
            estimated_start_at_ms=item.queued_at_ms + max(0, int((estimated - item.queued_at) * 1_000)),
        )

    def _next_wake_delay(self, current: float) -> float:
        deadlines = [self._next_global_at]
        deadlines.extend(self._next_proxy_at.get(item.proxy_key, 0.0) for item in self._queued)
        deadlines.extend(item.not_before for item in self._queued)
        future = [deadline for deadline in deadlines if deadline > current]
        return max(0.005, min(future) - current) if future else 0.05

    def _queue_position(self, key: str) -> int:
        return next(index + 1 for index, item in enumerate(self._queued) if item.key == key)

    def _queued_item(self, key: str) -> _QueuedPhase | None:
        return next((item for item in self._queued if item.key == key), None)

    def _remove_queued(self, key: str) -> None:
        item = self._queued_item(key)
        if item is not None:
            self._queued.remove(item)
            self._bump()

    def _stable_jitter(self, key: str) -> float:
        if self._stable_jitter_seconds == 0:
            return 0.0
        digest = hashlib.blake2s(key.encode("utf-8"), digest_size=8).digest()
        ratio = int.from_bytes(digest, "big") / ((1 << 64) - 1)
        return ratio * self._stable_jitter_seconds

    @staticmethod
    def _percentile(values: list[int], ratio: float) -> int:
        return values[int((len(values) - 1) * ratio)] if values else 0

    def _bump(self) -> None:
        self._revision += 1
        self._condition.notify_all()
