from __future__ import annotations

from fleet_api.fleet_write_coordinator import FleetWriteCoordinator


def test_critical_write_survives_idle_timeout() -> None:
    coordinator = FleetWriteCoordinator(low_priority_window_ms=1)
    try:
        assert coordinator.critical(lambda: "committed") == "committed"
        snapshot = coordinator.snapshot()
        assert snapshot.committed == 1
        assert snapshot.failed == 0
    finally:
        coordinator.close()


def test_low_priority_write_coalesces_to_the_latest_callback() -> None:
    coordinator = FleetWriteCoordinator(low_priority_window_ms=50)
    observed: list[str] = []
    try:
        superseded = coordinator.low_priority("execution:one", lambda: observed.append("old"))
        latest = coordinator.low_priority("execution:one", lambda: observed.append("new"))
        assert superseded.result(timeout=1) is None
        assert latest.result(timeout=1) is None
        assert observed == ["new"]
    finally:
        coordinator.close()


def test_close_drains_pending_low_priority_writes_and_rejects_new_work() -> None:
    coordinator = FleetWriteCoordinator(low_priority_window_ms=500)
    observed: list[str] = []
    coordinator.low_priority("execution:one", lambda: observed.append("drained"))
    coordinator.close()

    assert observed == ["drained"]
    try:
        coordinator.critical(lambda: None)
    except RuntimeError as error:
        assert "closed" in str(error)
    else:
        raise AssertionError("closed coordinator accepted a critical write")


def test_critical_write_does_not_flush_unrelated_low_priority_work() -> None:
    coordinator = FleetWriteCoordinator(low_priority_window_ms=500)
    observed: list[str] = []
    try:
        pending = coordinator.low_priority("execution:one", lambda: observed.append("heartbeat"))
        coordinator.critical(lambda: observed.append("boundary"))
        assert observed == ["boundary"]
        assert not pending.done()
    finally:
        coordinator.close()
    assert pending.result(timeout=1) is None
    assert observed == ["boundary", "heartbeat"]


def test_discarded_low_priority_write_does_not_consume_the_writer() -> None:
    coordinator = FleetWriteCoordinator(low_priority_window_ms=500)
    observed: list[str] = []
    try:
        pending = coordinator.low_priority("execution:one", lambda: observed.append("heartbeat"))

        assert coordinator.discard_low_priority("execution:one")
        assert pending.result(timeout=1) is None
        assert coordinator.critical(lambda: observed.append("boundary")) is None
        assert observed == ["boundary"]
    finally:
        coordinator.close()
