from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from threading import Event, RLock


@dataclass(frozen=True, slots=True)
class PhaseReservation:
    deadline_monotonic: float
    deadline_at_ms: int


class ExecutionPhasePacer:
    """Reserve interruptible execution slots across every worker in this executor."""

    def __init__(
        self,
        *,
        minimum_gap_seconds: float = 5,
        jitter_max_seconds: float = 15,
        monotonic: Callable[[], float] = time.monotonic,
        now_ms: Callable[[], int] = lambda: time.time_ns() // 1_000_000,
        randbelow: Callable[[int], int] = secrets.randbelow,
    ) -> None:
        if minimum_gap_seconds < 0 or jitter_max_seconds < 0:
            raise ValueError("phase pacing intervals cannot be negative")
        self._minimum_gap_seconds = minimum_gap_seconds
        self._jitter_max_ms = int(jitter_max_seconds * 1_000)
        self._monotonic = monotonic
        self._now_ms = now_ms
        self._randbelow = randbelow
        self._next_slot_monotonic = 0.0
        self._reservations: dict[str, PhaseReservation] = {}
        self._completed: set[str] = set()
        self._lock = RLock()

    def wait(
        self,
        key: str,
        *,
        phase: str,
        round_number: int,
        stop_event: Event,
        event_sink: Callable[[dict[str, object]], None],
    ) -> bool:
        reservation = self._reserve(key)
        if reservation is None:
            return True
        event_sink(
            {
                "event": "phase_pacing_started",
                "phase": phase,
                "round": round_number,
                "deadline_at_ms": reservation.deadline_at_ms,
            }
        )
        remaining = max(0.0, reservation.deadline_monotonic - self._monotonic())
        if stop_event.wait(remaining):
            event_sink(
                {
                    "event": "phase_pacing_cancelled",
                    "phase": phase,
                    "round": round_number,
                    "reason": "stop_requested",
                }
            )
            return False
        with self._lock:
            self._completed.add(key)
        event_sink(
            {
                "event": "phase_pacing_completed",
                "phase": phase,
                "round": round_number,
            }
        )
        return True

    def _reserve(self, key: str) -> PhaseReservation | None:
        if not key:
            raise ValueError("phase pacing key cannot be empty")
        with self._lock:
            if key in self._completed:
                return None
            existing = self._reservations.get(key)
            if existing is not None:
                return existing
            now = self._monotonic()
            jitter_ms = self._randbelow(self._jitter_max_ms + 1) if self._jitter_max_ms else 0
            deadline = max(now, self._next_slot_monotonic) + jitter_ms / 1_000
            reservation = PhaseReservation(
                deadline_monotonic=deadline,
                deadline_at_ms=self._now_ms() + max(0, int((deadline - now) * 1_000)),
            )
            self._reservations[key] = reservation
            self._next_slot_monotonic = deadline + self._minimum_gap_seconds
            return reservation
