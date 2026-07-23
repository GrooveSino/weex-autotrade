from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PollResult:
    processed: bool
    successful: bool


@dataclass(frozen=True, slots=True)
class GlobalStopAccountResult:
    stopped: bool
    cancel_verified: bool
    cancel_failed: bool

def session_projection_verified(session: dict[str, object]) -> bool:
    return (
        session.get("source_complete") is True
        and session.get("stale") is False
        and session.get("reconciliation_required") is False
        and session.get("pending_sync") is False
        and session.get("uncertain_order_state") is False
    )
