from __future__ import annotations

import threading
import time

from fleet_api.execution.runtime.execution_capacity import ExecutionCapacity


def test_active_admission_is_hard_limited_to_two_hundred() -> None:
    capacity = ExecutionCapacity(
        max_active_executions=200,
        max_normal_phases=20,
        phase_start_rate_per_second=4,
        per_proxy_gap_seconds=5,
    )

    assert all(capacity.admit(f"execution-{index}") for index in range(200))
    assert not capacity.admit("execution-200")
    assert capacity.snapshot().active_executions == 200

    capacity.release_execution("execution-0")
    assert capacity.admit("execution-200")


def test_proxy_cooldown_does_not_head_of_line_block_another_proxy() -> None:
    capacity = ExecutionCapacity(
        max_active_executions=3,
        max_normal_phases=2,
        phase_start_rate_per_second=10_000,
        per_proxy_gap_seconds=60,
    )
    stop = threading.Event()
    first = capacity.wait_for_phase("one:plan:1:open", proxy_key="proxy-a", stop_event=stop)
    assert first is not None
    capacity.enqueue_phase("two:plan:1:open", proxy_key="proxy-a")
    capacity.enqueue_phase("three:plan:1:open", proxy_key="proxy-b")

    acquired: list[str] = []

    def wait_for_other_proxy() -> None:
        result = capacity.wait_for_phase("three:plan:1:open", proxy_key="proxy-b", stop_event=stop)
        if result is not None:
            acquired.append(result.key)

    thread = threading.Thread(target=wait_for_other_proxy)
    thread.start()
    thread.join(timeout=1)

    assert not thread.is_alive()
    assert acquired == ["three:plan:1:open"]
    assert capacity.queue_state("two:plan:1:open") is not None


def test_stop_removes_a_queued_phase_without_consuming_a_slot() -> None:
    capacity = ExecutionCapacity(
        max_active_executions=2,
        max_normal_phases=1,
        phase_start_rate_per_second=1,
        per_proxy_gap_seconds=0,
    )
    first_stop = threading.Event()
    assert capacity.wait_for_phase("one:plan:1:open", proxy_key="proxy-a", stop_event=first_stop) is not None
    stopped = threading.Event()
    stopped.set()

    assert capacity.wait_for_phase("two:plan:1:open", proxy_key="proxy-b", stop_event=stopped) is None
    assert capacity.snapshot().queued_normal_phases == 0
    assert capacity.snapshot().active_normal_phases == 1


def test_stable_jitter_is_reused_for_a_reopened_queue_entry() -> None:
    capacity = ExecutionCapacity(
        max_active_executions=2,
        max_normal_phases=1,
        phase_start_rate_per_second=4,
        per_proxy_gap_seconds=0,
        stable_jitter_seconds=15,
    )
    initial = capacity.enqueue_phase("one:plan:1:open", proxy_key="proxy-a")
    repeated = capacity.enqueue_phase("one:plan:1:open", proxy_key="proxy-a")

    assert repeated.queue_position == initial.queue_position
    assert repeated.estimated_start_at_ms == initial.estimated_start_at_ms
    assert repeated.estimated_start_at_ms >= int(time.time() * 1000)


def test_idle_single_account_bypasses_stable_jitter() -> None:
    current = 100.0
    capacity = ExecutionCapacity(
        max_active_executions=200,
        max_normal_phases=20,
        phase_start_rate_per_second=4,
        per_proxy_gap_seconds=5,
        stable_jitter_seconds=15,
        now=lambda: current,
        now_ms=lambda: int(current * 1_000),
    )

    reservation = capacity.try_start_phase("one:1:open", proxy_key="proxy-a")

    assert reservation is not None
    assert reservation.estimated_start_at_ms == 100_000
    assert capacity.snapshot().queued_normal_phases == 0


def test_competing_account_keeps_stable_jitter() -> None:
    current = 100.0
    capacity = ExecutionCapacity(
        max_active_executions=200,
        max_normal_phases=20,
        phase_start_rate_per_second=10_000,
        per_proxy_gap_seconds=0,
        stable_jitter_seconds=15,
        now=lambda: current,
        now_ms=lambda: int(current * 1_000),
    )
    first = capacity.try_start_phase("one:1:open", proxy_key="proxy-a")
    assert first is not None

    queued = capacity.enqueue_phase("two:1:open", proxy_key="proxy-b")

    assert queued.estimated_start_at_ms > 100_000
    assert capacity.try_start_phase("two:1:open", proxy_key="proxy-b") is None


def test_nonblocking_phase_probe_does_not_create_a_waiting_thread() -> None:
    capacity = ExecutionCapacity(
        max_active_executions=2,
        max_normal_phases=1,
        phase_start_rate_per_second=10_000,
        per_proxy_gap_seconds=60,
    )

    first = capacity.try_start_phase("one:plan:1:open", proxy_key="proxy-a")
    assert first is not None
    assert capacity.try_start_phase("two:plan:1:open", proxy_key="proxy-a") is None
    assert capacity.queue_state("two:plan:1:open") is not None
    assert capacity.cancel_phase("two:plan:1:open")
    assert capacity.queue_state("two:plan:1:open") is None


def test_one_proxy_cannot_run_two_normal_phases_at_the_same_time() -> None:
    capacity = ExecutionCapacity(
        max_active_executions=3,
        max_normal_phases=2,
        phase_start_rate_per_second=10_000,
        per_proxy_gap_seconds=0,
    )

    first = capacity.try_start_phase("one:plan:1:open", proxy_key="proxy-a")
    assert first is not None
    assert capacity.try_start_phase("two:plan:1:open", proxy_key="proxy-a") is None
    time.sleep(0.002)
    assert capacity.try_start_phase("three:plan:1:open", proxy_key="proxy-b") is not None
    assert capacity.snapshot().active_proxy_partitions == 2
    capacity.finish_phase(first.key)
    time.sleep(0.002)
    assert capacity.try_start_phase("two:plan:1:open", proxy_key="proxy-a") is not None
