from __future__ import annotations

import time
from threading import Event

from fleet_api.execution.runtime.async_execution_orchestrator import AsyncExecutionOrchestrator
from fleet_api.execution.runtime.execution_capacity import ExecutionCapacity


def test_two_hundred_timer_actors_do_not_need_two_hundred_workers() -> None:
    capacity = ExecutionCapacity(
        max_active_executions=200,
        max_normal_phases=20,
        phase_start_rate_per_second=4,
        per_proxy_gap_seconds=5,
    )
    snapshots = []
    orchestrator = AsyncExecutionOrchestrator(
        capacity,
        normal_workers=2,
        emergency_workers=1,
        state_sink=snapshots.append,
    )
    release = Event()

    async def program(actor) -> None:  # type: ignore[no-untyped-def]
        actor.transition("holding", deadline_at_ms=int(time.time() * 1000) + 60_000)
        while not release.is_set() and not actor.stop_event.is_set():
            await actor.sleep_until(int(time.time() * 1000) + 10, phase="holding")
        actor.transition("stopped")

    try:
        futures = []
        for index in range(200):
            execution_id = f"execution-{index}"
            assert capacity.admit(execution_id)
            futures.append(orchestrator.start(execution_id, f"account-{index}", program))
        deadline = time.monotonic() + 3
        while (orchestrator.active_count() < 200 or len(snapshots) < 200) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert orchestrator.active_count() == 200
        assert capacity.snapshot().active_executions == 200
        assert snapshots
    finally:
        release.set()
        for index in range(200):
            orchestrator.stop(f"execution-{index}")
        for future in futures:
            future.result(timeout=5)
        orchestrator.close()


def test_phase_wait_is_cancellable_without_a_blocking_queue_thread() -> None:
    capacity = ExecutionCapacity(
        max_active_executions=2,
        max_normal_phases=1,
        phase_start_rate_per_second=1,
        per_proxy_gap_seconds=0,
    )
    orchestrator = AsyncExecutionOrchestrator(capacity, normal_workers=1, emergency_workers=1)

    async def first(actor) -> None:  # type: ignore[no-untyped-def]
        reservation = await actor.wait_for_normal_phase("open", proxy_key="proxy-a", round_number=1)
        assert reservation is not None
        await actor.sleep_until(int(time.time() * 1000) + 5_000, phase="holding")
        actor.finish_normal_phase(reservation)
        actor.transition("stopped")

    async def second(actor) -> None:  # type: ignore[no-untyped-def]
        reservation = await actor.wait_for_normal_phase("open", proxy_key="proxy-b", round_number=1)
        assert reservation is None
        actor.transition("stopped")

    try:
        assert capacity.admit("one")
        first_future = orchestrator.start("one", "account-one", first)
        deadline = time.monotonic() + 2
        while capacity.snapshot().active_normal_phases != 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert capacity.admit("two")
        second_future = orchestrator.start("two", "account-two", second)
        deadline = time.monotonic() + 2
        while capacity.snapshot().queued_normal_phases != 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        orchestrator.stop("two")
        second_future.result(timeout=3)
        assert capacity.snapshot().queued_normal_phases == 0
    finally:
        orchestrator.stop("one")
        first_future.result(timeout=3)
        orchestrator.close()


def test_phase_queue_state_is_not_republished_on_each_capacity_probe() -> None:
    capacity = ExecutionCapacity(
        max_active_executions=2,
        max_normal_phases=1,
        phase_start_rate_per_second=1,
        per_proxy_gap_seconds=0,
    )
    states = []
    orchestrator = AsyncExecutionOrchestrator(
        capacity,
        normal_workers=1,
        emergency_workers=1,
        state_sink=states.append,
    )

    async def occupy(actor) -> None:  # type: ignore[no-untyped-def]
        reservation = await actor.wait_for_normal_phase("open", proxy_key="proxy-a", round_number=1)
        assert reservation is not None
        await actor.sleep_until(int(time.time() * 1_000) + 2_000, phase="holding")
        actor.finish_normal_phase(reservation)

    async def queue(actor) -> None:  # type: ignore[no-untyped-def]
        await actor.wait_for_normal_phase("open", proxy_key="proxy-b", round_number=1)

    try:
        assert capacity.admit("one")
        first = orchestrator.start("one", "account-one", occupy)
        deadline = time.monotonic() + 2
        while capacity.snapshot().active_normal_phases != 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert capacity.admit("two")
        second = orchestrator.start("two", "account-two", queue)
        time.sleep(0.7)
        queued = [state for state in states if state.execution_id == "two" and state.phase == "phase_queued"]
        assert len(queued) <= 2
        orchestrator.stop("two")
        second.result(timeout=3)
    finally:
        orchestrator.stop("one")
        first.result(timeout=3)
        orchestrator.close()


def test_observer_failure_does_not_interrupt_a_stopped_actor() -> None:
    capacity = ExecutionCapacity(
        max_active_executions=1,
        max_normal_phases=1,
        phase_start_rate_per_second=1,
        per_proxy_gap_seconds=0,
    )

    def broken_observer(_state: object) -> None:
        raise RuntimeError("sqlite unavailable")

    orchestrator = AsyncExecutionOrchestrator(
        capacity,
        normal_workers=1,
        emergency_workers=1,
        state_sink=broken_observer,
    )

    async def program(actor) -> None:  # type: ignore[no-untyped-def]
        await actor.sleep_until(int(time.time() * 1_000) + 2_000, phase="holding")
        actor.transition("stopped")

    try:
        assert capacity.admit("one")
        future = orchestrator.start("one", "account-one", program)
        time.sleep(0.02)
        assert orchestrator.stop("one")
        future.result(timeout=3)
        assert capacity.snapshot().active_executions == 0
    finally:
        orchestrator.close()
