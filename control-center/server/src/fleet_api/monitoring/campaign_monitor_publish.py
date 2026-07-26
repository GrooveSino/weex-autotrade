"""Bounded monitor publication that never changes a Campaign outcome."""

from __future__ import annotations

from concurrent.futures import Future
from typing import Any

from weex_cli.control_api.progress import EXECUTION_PROGRESS_PROJECTION_VERSION

from fleet_api.auth.ownership import LEGACY_OWNER_USER_ID
from fleet_api.campaigns.core.campaign_contracts import CampaignRecord
from fleet_api.campaigns.core.event_projection import publishes_fleet_snapshot

_COALESCED_EVENTS = frozenset({"leg_progress", "leg_waiting", "pair_wait_progress"})


def publish_monitor_event(manager: Any, record: CampaignRecord, event: dict[str, Any]) -> None:
    """Publish durable monitor data after it commits, coalescing wait heartbeats."""
    if str(event.get("name") or "") not in _COALESCED_EVENTS:
        # A state boundary supersedes an uncommitted wait heartbeat from this
        # execution.  Dropping that obsolete write keeps it from resurrecting
        # an active wait after a completion/cancel event, and it lets critical
        # order boundaries bypass unrelated accounts' heartbeat backlogs.
        manager.write_coordinator.discard_low_priority(f"monitor:{record.campaign_id}:heartbeat")
        _publish_committed(manager, record, event, manager._append_monitor_event(record, event))
        return
    key = f"monitor:{record.campaign_id}:heartbeat"
    future = manager.write_coordinator.low_priority(
        key,
        lambda: append_monitor_event_direct(manager, record, event),
    )
    future.add_done_callback(lambda completed: _publish_future(manager, record, event, completed))


def _publish_future(
    manager: Any,
    record: CampaignRecord,
    event: dict[str, Any],
    future: Future[Any],
) -> None:
    try:
        sequence = future.result()
    except Exception:
        return
    if isinstance(sequence, int):
        _publish_committed(manager, record, event, sequence)


def append_monitor_event_direct(manager: Any, record: CampaignRecord, event: dict[str, Any]) -> int:
    """Write one monitor journal entry while already inside the coordinator."""
    return manager.journal.append_and_project(
        record.campaign_id,
        event,
        owner_user_id=str(record.metadata.get("owner_user_id") or LEGACY_OWNER_USER_ID),
        account_id=record.instance_id,
        session_id=str(record.metadata["session_id"]) if record.metadata.get("session_id") else None,
        executor_generation=manager.executor_generation,
        projection_version=EXECUTION_PROGRESS_PROJECTION_VERSION,
    )


def _publish_committed(manager: Any, record: CampaignRecord, event: dict[str, Any], sequence: int) -> None:
    event["sequence"] = sequence
    manager._notify_progress(record.instance_id, event)
    if publishes_fleet_snapshot(str(event["name"])):
        manager._notify(record.instance_id)
