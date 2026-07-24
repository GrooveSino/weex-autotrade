from __future__ import annotations

import threading
import time

from fleet_api.async_execution_orchestrator import AsyncExecutionOrchestrator
from fleet_api.execution_capacity import ExecutionCapacity
from fleet_api.executor_process_metrics import process_snapshot


def test_two_hundred_account_lifecycles_respect_phase_and_i_o_budgets() -> None:
    capacity = ExecutionCapacity(
        max_active_executions=200,
        max_normal_phases=20,
        phase_start_rate_per_second=10_000,
        per_proxy_gap_seconds=0,
    )
    runtime = AsyncExecutionOrchestrator(capacity, normal_workers=64, emergency_workers=32)
    opened = [threading.Event() for _ in range(200)]
    lock = threading.Lock()
    active_phase = 0
    peak_phase = 0
    active_io = 0
    peak_io = 0

    async def program(actor, index: int) -> None:  # type: ignore[no-untyped-def]
        nonlocal active_phase, peak_phase, active_io, peak_io
        reservation = await actor.wait_for_normal_phase("open", proxy_key=f"proxy-{index}", round_number=1)
        assert reservation is not None

        def open_phase() -> None:
            nonlocal active_phase, peak_phase, active_io, peak_io
            with lock:
                active_phase += 1
                active_io += 1
                peak_phase = max(peak_phase, active_phase)
                peak_io = max(peak_io, active_io)
            time.sleep(0.004)
            with lock:
                active_io -= 1
                active_phase -= 1

        try:
            await actor.run_blocking(open_phase)
        finally:
            actor.finish_normal_phase(reservation)
        opened[index].set()
        await actor.sleep_until(int(time.time() * 1_000) + 60_000, phase="holding", reason="soak")

    futures = []
    try:
        for index in range(200):
            execution_id = f"soak-{index}"
            assert capacity.admit(execution_id)
            futures.append(runtime.start(execution_id, f"account-{index}", lambda actor, i=index: program(actor, i)))
        deadline = time.monotonic() + 8
        while not all(event.is_set() for event in opened) and time.monotonic() < deadline:
            time.sleep(0.01)
        runtime_snapshot = runtime.snapshot()
        process = process_snapshot()

        assert all(event.is_set() for event in opened)
        assert capacity.snapshot().active_executions == 200
        assert runtime_snapshot.actor_count == 200
        assert peak_phase <= 20
        assert peak_io <= 64
        assert runtime_snapshot.event_loop_delay_p99_ms < 100
        assert process.open_file_descriptors < 2_000
        assert process.rss_bytes < 2_684_354_560
        assert not capacity.admit("soak-201")
    finally:
        for index in range(200):
            runtime.stop(f"soak-{index}")
        for future in futures:
            future.result(timeout=8)
        runtime.close()
