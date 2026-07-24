"""Read-only projection of the single strategy-run lifecycle."""

from __future__ import annotations

from .campaign_contracts import CampaignJournal
from .models import ExecutionLifecycleSnapshot
from .volume_contracts import TradeVolumeLedger


def project_strategy_run_lifecycle(
    journal: CampaignJournal,
    ledger: TradeVolumeLedger,
    instance_id: str,
    mode: str,
) -> ExecutionLifecycleSnapshot:
    record = journal.active_for_instance(instance_id)
    session = ledger.active_session(instance_id, mode)
    if record is not None:
        boundary = journal.boundary_projection(instance_id) or {}
        state = {
            "planned": ("preparing", "start"),
            "executing": ("running", "stop"),
            "stopping": ("stopping", "wait"),
            "recovering": (
                ("recovery_cleanup_required", "safe_stop")
                if record.metadata.get("recovery_state") == "cleanup_required"
                else ("recovering", "recheck")
            ),
        }.get(record.status)
        if state is not None:
            return ExecutionLifecycleSnapshot(
                state=state[0],
                primary_action=state[1],
                execution_id=record.campaign_id,
                session_id=str(record.metadata.get("session_id") or "") or None,
                reason_code=str(record.metadata.get("reason") or "") or None,
                position_count=int(boundary.get("position_count") or 0),
                regular_order_count=int(boundary.get("regular_order_count") or 0),
                trigger_order_count=int(boundary.get("trigger_order_count") or 0),
                blocking_positions=list(boundary.get("blocking_positions") or []),
                boundary_checked_at_ms=int(boundary.get("checked_at_ms") or 0) or None,
            )
    if session is not None and str(session.get("status")) in {"recovering", "stopping", "active"}:
        status = str(session["status"])
        state, action = {"recovering": ("recovering", "recheck"), "stopping": ("stopping", "wait")}.get(
            status, ("running", "stop")
        )
        return ExecutionLifecycleSnapshot(state=state, primary_action=action, session_id=str(session["session_id"]))
    boundary = journal.boundary_projection(instance_id)
    if boundary is not None:
        positions = int(boundary.get("position_count") or 0)
        regular = int(boundary.get("regular_order_count") or 0)
        triggers = int(boundary.get("trigger_order_count") or 0)
        if positions or regular or triggers:
            has_orders = regular > 0 or triggers > 0
            return ExecutionLifecycleSnapshot(
                state="orders_cleanup_required" if has_orders else "position_blocked",
                primary_action="cancel_orders" if has_orders else "recheck",
                reason_code="launch_orders_present" if has_orders else "launch_positions_present",
                position_count=positions,
                regular_order_count=regular,
                trigger_order_count=triggers,
                blocking_positions=list(boundary.get("blocking_positions") or []),
                boundary_checked_at_ms=int(boundary.get("checked_at_ms") or 0) or None,
            )
    return ExecutionLifecycleSnapshot()
