"""Project durable history-sync checkpoints into a safe account read model."""

from __future__ import annotations

from typing import Any

from .models_account import HistorySyncProjection


def project_history_sync(checkpoint: dict[str, Any] | None) -> HistorySyncProjection:
    """Return a presentation-only projection without exposing source internals."""
    if checkpoint is None:
        return HistorySyncProjection()
    baseline = _baseline(checkpoint.get("initial_baseline_state"))
    reason = checkpoint.get("sync_reason")
    safe_reason = reason if isinstance(reason, str) and len(reason) <= 40 else None
    state = _state(checkpoint, baseline)
    return HistorySyncProjection(
        state=state,
        reason=safe_reason,
        initial_baseline_state=baseline,
        pending=bool(checkpoint.get("pending")),
        source_complete=bool(checkpoint.get("source_complete")),
        stale=bool(checkpoint.get("stale")),
        last_success_at_ms=_timestamp(checkpoint.get("last_success_at_ms")),
        next_sync_at_ms=_timestamp(checkpoint.get("next_sync_at_ms")),
        high_watermark_ms=_timestamp(checkpoint.get("high_watermark_ms")),
    )


def _state(checkpoint: dict[str, Any], baseline: str) -> str:
    if baseline == "queued":
        return "initial_baseline_queued"
    if baseline == "running":
        return "initial_baseline_running"
    if baseline == "pending":
        return "initial_baseline_pending"
    if checkpoint.get("next_sync_at_ms") is not None:
        return "incremental_queued"
    if checkpoint.get("sync_reason") and not checkpoint.get("last_success_at_ms"):
        return "syncing"
    if bool(checkpoint.get("stale")):
        return "stale"
    if bool(checkpoint.get("source_complete")):
        return "fresh"
    return "not_requested"


def _baseline(value: object) -> str:
    return value if value in {"not_requested", "queued", "running", "complete", "pending"} else "not_requested"


def _timestamp(value: object) -> int | None:
    return value if isinstance(value, int) and value >= 0 else None
