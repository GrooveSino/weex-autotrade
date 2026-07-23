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
        state = {
            "planned": ("preparing", "start"),
            "executing": ("running", "stop"),
            "stopping": ("stopping", "wait"),
            "recovering": ("recovering", "wait"),
        }.get(record.status)
        if state is not None:
            if record.status == "recovering" and record.metadata.get("cleanup_required"):
                state = ("cleanup_required", "cleanup")
            return ExecutionLifecycleSnapshot(
                state=state[0],
                primary_action=state[1],
                execution_id=record.campaign_id,
                session_id=str(record.metadata.get("session_id") or "") or None,
                reason_code=str(record.metadata.get("reason") or "") or None,
                position_count=int(record.metadata.get("position_count") or 0),
                regular_order_count=int(record.metadata.get("regular_order_count") or 0),
                trigger_order_count=int(record.metadata.get("trigger_order_count") or 0),
            )
    if session is not None and str(session.get("status")) in {"recovering", "stopping", "active"}:
        status = str(session["status"])
        state, action = {"recovering": ("recovering", "wait"), "stopping": ("stopping", "wait")}.get(
            status, ("running", "stop")
        )
        return ExecutionLifecycleSnapshot(state=state, primary_action=action, session_id=str(session["session_id"]))
    return ExecutionLifecycleSnapshot()
