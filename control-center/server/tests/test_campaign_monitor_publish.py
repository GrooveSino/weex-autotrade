from __future__ import annotations

from threading import Event
from types import SimpleNamespace

from fleet_api.campaign_monitor_publish import publish_monitor_event
from fleet_api.fleet_write_coordinator import FleetWriteCoordinator


class _Journal:
    def __init__(self) -> None:
        self.events: list[str] = []

    def append_and_project(self, _campaign_id: str, event: dict[str, object], **_kwargs: object) -> int:
        self.events.append(str(event["name"]))
        return len(self.events)


class _Manager:
    def __init__(self) -> None:
        self.journal = _Journal()
        self.write_coordinator = FleetWriteCoordinator(low_priority_window_ms=20)
        self.executor_generation = "test-generation"
        self.progress: list[dict[str, object]] = []
        self.progressed = Event()

    def _append_monitor_event(self, record: object, event: dict[str, object]) -> int:
        return self.write_coordinator.critical(
            lambda: self.journal.append_and_project(record.campaign_id, event)  # type: ignore[attr-defined]
        )

    def _notify_progress(self, _instance_id: str, event: dict[str, object]) -> None:
        self.progress.append(dict(event))
        self.progressed.set()

    def _notify(self, _instance_id: str) -> None:
        return

    def close(self) -> None:
        self.write_coordinator.close()


def test_wait_heartbeats_coalesce_before_they_persist_or_notify() -> None:
    manager = _Manager()
    record = SimpleNamespace(campaign_id="execution-1", instance_id="account-1", metadata={})
    try:
        publish_monitor_event(manager, record, {"name": "leg_waiting"})
        publish_monitor_event(manager, record, {"name": "leg_progress"})

        assert manager.progressed.wait(timeout=1)
        assert manager.journal.events == ["leg_progress"]
        assert [event["name"] for event in manager.progress] == ["leg_progress"]
        assert manager.progress[0]["sequence"] == 1
    finally:
        manager.close()


def test_boundary_write_flushes_prior_heartbeat_before_it_persists() -> None:
    manager = _Manager()
    record = SimpleNamespace(campaign_id="execution-1", instance_id="account-1", metadata={})
    try:
        publish_monitor_event(manager, record, {"name": "leg_progress"})
        publish_monitor_event(manager, record, {"name": "leg_completed"})

        assert manager.progressed.wait(timeout=1)
        assert manager.journal.events == ["leg_progress", "leg_completed"]
        assert [event["name"] for event in manager.progress] == ["leg_progress", "leg_completed"]
    finally:
        manager.close()
