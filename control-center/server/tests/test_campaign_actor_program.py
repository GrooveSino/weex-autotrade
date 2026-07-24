from __future__ import annotations

import threading
import time
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from fleet_api.async_execution_orchestrator import AsyncExecutionOrchestrator
from fleet_api.campaign_actor_models import CampaignActorContext, CloseCycle, OpenCycle
from fleet_api.campaign_actor_program import CampaignActorProgram
from fleet_api.execution_capacity import ExecutionCapacity


def _context() -> CampaignActorContext:
    child = SimpleNamespace(estimated_rounds=1, max_empty_rounds=1)
    return CampaignActorContext(child=child, run_number=1, execution_started_at_ms=1_000)


class _Phases:
    def __init__(
        self,
        *,
        hold_seconds: float,
        finish_status: str = "stopped",
        open_pause: float = 0,
        open_error: Exception | None = None,
        close_error: Exception | None = None,
    ) -> None:
        self.hold_seconds = hold_seconds
        self.finish_status = finish_status
        self.open_pause = open_pause
        self.open_error = open_error
        self.close_error = close_error
        self.opened = threading.Event()
        self.safe_calls = 0
        self.active_opens = 0
        self.peak_opens = 0
        self._lock = threading.Lock()

    def prepare(self, _campaign: object) -> CampaignActorContext:
        return _context()

    def plan_open(self, _campaign: object, context: CampaignActorContext) -> OpenCycle:
        with self._lock:
            self.active_opens += 1
            self.peak_opens = max(self.peak_opens, self.active_opens)
        try:
            if self.open_pause:
                time.sleep(self.open_pause)
            return OpenCycle(
                context,
                {},
                None,  # type: ignore[arg-type]
                None,  # type: ignore[arg-type]
                {},
                400,
                {},
                [],
                {},
                int(time.time() * 1_000),
                self.hold_seconds,
            )
        finally:
            with self._lock:
                self.active_opens -= 1

    def execute_open(self, _campaign: object, _opened: OpenCycle) -> None:
        self.opened.set()
        if self.open_error is not None:
            raise self.open_error

    def close(self, _campaign: object, _opened: OpenCycle) -> CloseCycle:
        if self.close_error is not None:
            raise self.close_error
        return CloseCycle(Decimal(0), {"status": "completed", "reason": "done"}, None, None, 0)

    def safe_stop(self, _opened: OpenCycle) -> dict[str, str]:
        self.safe_calls += 1
        return {"status": "stopped", "reason": "stop_requested"}

    def finish(self, _campaign: object, _context: CampaignActorContext, **_kwargs: object) -> dict[str, str]:
        return {"status": self.finish_status, "reason": "finished"}


def _program(
    phases: _Phases,
    result: list[dict[str, Any]],
    *,
    proxy_key: str = "proxy-a",
    failures: list[Exception] | None = None,
) -> CampaignActorProgram:
    return CampaignActorProgram(
        SimpleNamespace(),  # type: ignore[arg-type]
        phases,  # type: ignore[arg-type]
        proxy_key=proxy_key,
        on_result=result.append,
        on_failure=(failures.append if failures is not None else lambda _error: None),
    )


def test_stop_from_holding_uses_emergency_path_without_waiting_for_normal_slot() -> None:
    capacity = ExecutionCapacity(
        max_active_executions=2,
        max_normal_phases=1,
        phase_start_rate_per_second=10_000,
        per_proxy_gap_seconds=0,
    )
    states = []
    runtime = AsyncExecutionOrchestrator(capacity, normal_workers=1, emergency_workers=1, state_sink=states.append)
    phases = _Phases(hold_seconds=60, finish_status="uncertain")
    result: list[dict[str, Any]] = []
    failures: list[Exception] = []
    assert capacity.admit("one")
    future = runtime.start("one", "account-one", _program(phases, result, failures=failures))
    try:
        assert phases.opened.wait(timeout=2)
        time.sleep(0.002)
        blocker = capacity.try_start_phase("other:1:close", proxy_key="proxy-b")
        assert blocker is not None
        assert runtime.stop("one")
        future.result(timeout=3)
        assert phases.safe_calls == 1
        assert not failures
        assert result == [{"status": "uncertain", "reason": "finished"}]
        assert any(state.phase == "stopping" for state in states)
        assert states[-1].phase == "recovering"
    finally:
        if "blocker" in locals() and blocker is not None:
            capacity.finish_phase(blocker.key)
        runtime.close()


def test_stop_while_waiting_for_close_slot_keeps_open_cycle_for_safe_stop(monkeypatch) -> None:
    capacity = ExecutionCapacity(
        max_active_executions=2,
        max_normal_phases=1,
        phase_start_rate_per_second=10_000,
        per_proxy_gap_seconds=0,
    )
    runtime = AsyncExecutionOrchestrator(capacity, normal_workers=1, emergency_workers=1)
    phases = _Phases(hold_seconds=0)
    result: list[dict[str, Any]] = []
    assert capacity.admit("one")
    original_try_start = capacity.try_start_phase
    close_waiting = threading.Event()

    def block_close_slot(key: str, *, proxy_key: str):
        if key == "one:1:close":
            close_waiting.set()
            return None
        return original_try_start(key, proxy_key=proxy_key)

    monkeypatch.setattr(capacity, "try_start_phase", block_close_slot)
    future = runtime.start("one", "account-one", _program(phases, result))
    try:
        assert phases.opened.wait(timeout=2)
        assert close_waiting.wait(timeout=2)
        assert runtime.stop("one")
        future.result(timeout=3)
        assert phases.safe_calls == 1
        assert result == [{"status": "stopped", "reason": "finished"}]
    finally:
        runtime.close()


def test_close_phase_error_after_open_uses_emergency_safe_stop() -> None:
    capacity = ExecutionCapacity(
        max_active_executions=1,
        max_normal_phases=1,
        phase_start_rate_per_second=10_000,
        per_proxy_gap_seconds=0,
    )
    runtime = AsyncExecutionOrchestrator(capacity, normal_workers=1, emergency_workers=1)
    phases = _Phases(hold_seconds=0, close_error=RuntimeError("close broke"))
    result: list[dict[str, Any]] = []
    failures: list[Exception] = []
    assert capacity.admit("one")
    future = runtime.start("one", "account-one", _program(phases, result, failures=failures))
    try:
        future.result(timeout=3)
        assert phases.safe_calls == 1
        assert result == [{"status": "stopped", "reason": "finished"}]
        assert not failures
    finally:
        runtime.close()


def test_open_barrier_error_after_plan_uses_emergency_safe_stop() -> None:
    capacity = ExecutionCapacity(
        max_active_executions=1,
        max_normal_phases=1,
        phase_start_rate_per_second=10_000,
        per_proxy_gap_seconds=0,
    )
    runtime = AsyncExecutionOrchestrator(capacity, normal_workers=1, emergency_workers=1)
    phases = _Phases(hold_seconds=0, open_error=TypeError("position comparison failed"))
    result: list[dict[str, Any]] = []
    failures: list[Exception] = []
    assert capacity.admit("one")
    future = runtime.start("one", "account-one", _program(phases, result, failures=failures))
    try:
        future.result(timeout=3)
        assert phases.safe_calls == 1
        assert result == [{"status": "stopped", "reason": "finished"}]
        assert not failures
    finally:
        runtime.close()


def test_orchestrator_shutdown_waits_for_safe_stop_instead_of_cancelling_actor() -> None:
    capacity = ExecutionCapacity(
        max_active_executions=1,
        max_normal_phases=1,
        phase_start_rate_per_second=10_000,
        per_proxy_gap_seconds=0,
    )
    states = []
    runtime = AsyncExecutionOrchestrator(capacity, normal_workers=1, emergency_workers=1, state_sink=states.append)
    phases = _Phases(hold_seconds=60)
    result: list[dict[str, Any]] = []
    assert capacity.admit("one")
    runtime.start("one", "account-one", _program(phases, result))
    try:
        assert phases.opened.wait(timeout=2)
        deadline = time.monotonic() + 2
        while not any(state.phase == "holding" for state in states):
            assert time.monotonic() < deadline
            time.sleep(0.01)

        runtime.close()

        assert phases.safe_calls == 1
        assert result == [{"status": "stopped", "reason": "finished"}]
    finally:
        runtime.close()


def test_two_hundred_campaign_programs_hold_without_consuming_two_hundred_i_o_workers() -> None:
    capacity = ExecutionCapacity(
        max_active_executions=200,
        max_normal_phases=20,
        phase_start_rate_per_second=10_000,
        per_proxy_gap_seconds=0,
    )
    runtime = AsyncExecutionOrchestrator(capacity, normal_workers=8, emergency_workers=32)
    phases = [_Phases(hold_seconds=60, open_pause=0.002) for _ in range(200)]
    results: list[dict[str, Any]] = []
    futures = []
    try:
        for index, phase in enumerate(phases):
            execution_id = f"execution-{index}"
            assert capacity.admit(execution_id)
            futures.append(
                runtime.start(execution_id, f"account-{index}", _program(phase, results, proxy_key=f"proxy-{index}"))
            )
        deadline = time.monotonic() + 8
        while not all(phase.opened.is_set() for phase in phases) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert runtime.active_count() == 200
        assert max(phase.peak_opens for phase in phases) <= 8
        assert all(phase.opened.is_set() for phase in phases)
        # ``opened`` is set inside the blocking phase before its coroutine
        # resumes and releases the scheduler reservation.  Wait for that
        # hand-off instead of asserting a racy instantaneous zero.
        while capacity.snapshot().active_normal_phases and time.monotonic() < deadline:
            time.sleep(0.01)
        assert capacity.snapshot().active_normal_phases == 0
    finally:
        for index in range(200):
            runtime.stop(f"execution-{index}")
        for future in futures:
            future.result(timeout=10)
        runtime.close()
