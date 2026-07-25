"""Compatibility facade for Fleet's bounded normal execution-phase scheduler."""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from threading import Event

from fleet_api.execution.runtime.execution_capacity import ExecutionCapacity, ExecutionCapacitySnapshot


class ExecutionPhasePacer:
    """Expose a synchronous phase gate without hiding queueing as execution."""

    def __init__(
        self,
        *,
        capacity: ExecutionCapacity | None = None,
        minimum_gap_seconds: float | None = None,
        jitter_max_seconds: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        now_ms: Callable[[], int] = lambda: time.time_ns() // 1_000_000,
        randbelow: Callable[[int], int] = secrets.randbelow,
    ) -> None:
        if capacity is None and (minimum_gap_seconds is None or jitter_max_seconds is None):
            raise TypeError("capacity or legacy phase timing arguments are required")
        self._capacity = capacity
        self._now_ms = now_ms
        self._legacy_gap_seconds = minimum_gap_seconds
        self._legacy_jitter_max_ms = int((jitter_max_seconds or 0) * 1_000)
        self._monotonic = monotonic
        self._randbelow = randbelow
        self._legacy_next_slot = 0.0
        self._legacy_deadlines: dict[str, _LegacyReservation] = {}
        self._legacy_completed: set[str] = set()

    def wait(
        self,
        key: str,
        *,
        phase: str,
        round_number: int,
        proxy_key: str = "direct",
        stop_event: Event,
        event_sink: Callable[[dict[str, object]], None],
    ) -> bool:
        if self._capacity is None:
            return self._wait_legacy(key, phase, round_number, stop_event, event_sink)
        queued = self._capacity.enqueue_phase(key, proxy_key=proxy_key)
        event_sink(
            {
                "event": "phase_pacing_started",
                "phase": phase,
                "round": round_number,
                "queue_position": queued.queue_position,
                "started_at_ms": queued.queued_at_ms,
                "deadline_at_ms": queued.estimated_start_at_ms,
            }
        )
        reservation = self._capacity.wait_for_phase(key, proxy_key=proxy_key, stop_event=stop_event)
        if reservation is None:
            event_sink(
                {
                    "event": "phase_pacing_cancelled",
                    "phase": phase,
                    "round": round_number,
                    "reason": "stop_requested",
                }
            )
            return False
        event_sink(
            {
                "event": "phase_pacing_completed",
                "phase": phase,
                "round": round_number,
                "queue_position": reservation.queue_position,
                "started_at_ms": self._now_ms(),
            }
        )
        return True

    def finish(self, key: str) -> None:
        if self._capacity is not None:
            self._capacity.finish_phase(key)

    def release_execution(self, execution_id: str) -> None:
        if self._capacity is not None:
            self._capacity.release_execution(execution_id)

    def snapshot(self) -> ExecutionCapacitySnapshot:
        if self._capacity is None:
            raise RuntimeError("legacy phase pacer does not expose capacity")
        return self._capacity.snapshot()

    def _wait_legacy(
        self,
        key: str,
        phase: str,
        round_number: int,
        stop_event: Event,
        event_sink: Callable[[dict[str, object]], None],
    ) -> bool:
        if key in self._legacy_completed:
            return True
        reservation = self._legacy_deadlines.get(key)
        if reservation is None:
            now = self._monotonic()
            jitter_ms = self._randbelow(self._legacy_jitter_max_ms + 1) if self._legacy_jitter_max_ms else 0
            deadline = max(now, self._legacy_next_slot) + jitter_ms / 1_000
            reservation = _LegacyReservation(deadline, self._now_ms() + max(0, int((deadline - now) * 1_000)))
            self._legacy_deadlines[key] = reservation
            self._legacy_next_slot = deadline + float(self._legacy_gap_seconds)
        event_sink(
            {
                "event": "phase_pacing_started",
                "phase": phase,
                "round": round_number,
                "deadline_at_ms": reservation.deadline_at_ms,
            }
        )
        if stop_event.wait(max(0.0, reservation.deadline_monotonic - self._monotonic())):
            event_sink({"event": "phase_pacing_cancelled", "phase": phase, "round": round_number})
            return False
        self._legacy_completed.add(key)
        event_sink({"event": "phase_pacing_completed", "phase": phase, "round": round_number})
        return True


@dataclass(frozen=True, slots=True)
class _LegacyReservation:
    deadline_monotonic: float
    deadline_at_ms: int
