"""Persistent, user-visible lifecycle state for a single Fleet execution."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

ActorPhase = Literal[
    "admitted",
    "preparing",
    "phase_queued",
    "opening",
    "holding",
    "closing",
    "stopping",
    "recovering",
    "completed",
    "stopped",
    "failed",
]

TERMINAL_ACTOR_PHASES = frozenset({"completed", "stopped", "failed"})


@dataclass(frozen=True, slots=True)
class ActorPhaseQueue:
    phase: str
    queue_position: int
    estimated_start_at_ms: int
    proxy_key: str
    constraint: str = "queued"


@dataclass(frozen=True, slots=True)
class ExecutionActorState:
    execution_id: str
    account_id: str
    phase: ActorPhase
    updated_at_ms: int
    wait_deadline_at_ms: int | None = None
    phase_queue: ActorPhaseQueue | None = None
    reason: str | None = None

    @property
    def terminal(self) -> bool:
        return self.phase in TERMINAL_ACTOR_PHASES

    def as_dict(self) -> dict[str, object]:
        return asdict(self)
