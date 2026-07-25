from __future__ import annotations

import threading
import time
from decimal import Decimal
from types import SimpleNamespace

from fleet_api.campaigns.actors.campaign_actor_models import CampaignActorContext, CloseCycle, OpenCycle
from fleet_api.campaigns.actors.campaign_actor_program import CampaignActorProgram
from fleet_api.execution.runtime.async_execution_orchestrator import AsyncExecutionOrchestrator
from fleet_api.execution.runtime.execution_capacity import ExecutionCapacity


class _Market:
    enabled = True

    def __init__(self) -> None:
        self.ready = threading.Event()
        self.waiting: set[str] = set()

    def fresh(self) -> bool:
        return self.ready.is_set()

    def set_waiting(self, execution_id: str, waiting: bool) -> None:
        (self.waiting.add if waiting else self.waiting.discard)(execution_id)


class _Phases:
    def __init__(self) -> None:
        self.opened = threading.Event()

    def prepare(self, _campaign: object) -> CampaignActorContext:
        child = SimpleNamespace(estimated_rounds=1, max_empty_rounds=1)
        return CampaignActorContext(child=child, run_number=1, execution_started_at_ms=1)

    def plan_open(self, _campaign: object, context: CampaignActorContext) -> OpenCycle:
        return OpenCycle(context, {}, None, None, {}, 1, {}, [], {}, 1, 0)  # type: ignore[arg-type]

    def check_open_conditions(self, _context: CampaignActorContext) -> None:
        return

    def execute_open(self, _campaign: object, _opened: OpenCycle) -> None:
        self.opened.set()

    def close(self, _campaign: object, _opened: OpenCycle) -> CloseCycle:
        return CloseCycle(Decimal(0), {"status": "completed", "reason": "done"}, None, None, 0)

    def finish(self, *_args: object, **_kwargs: object) -> dict[str, str]:
        return {"status": "stopped", "reason": "finished"}


def test_shared_market_recovery_waits_before_normal_open_slot() -> None:
    capacity = ExecutionCapacity(
        max_active_executions=1,
        max_normal_phases=1,
        phase_start_rate_per_second=10_000,
        per_proxy_gap_seconds=0,
    )
    states = []
    runtime = AsyncExecutionOrchestrator(capacity, normal_workers=1, emergency_workers=1, state_sink=states.append)
    phases, market, result = _Phases(), _Market(), []
    program = CampaignActorProgram(
        SimpleNamespace(),
        phases,
        proxy_key="proxy",
        shared_market=market,
        on_result=result.append,
        on_failure=lambda _error: None,
        on_event=lambda _event: None,
    )
    assert capacity.admit("one")
    future = runtime.start("one", "account-one", program)
    try:
        _wait(lambda: any(state.phase == "condition_waiting" for state in states))
        assert not phases.opened.is_set()
        assert capacity.snapshot().active_normal_phases == 0
        assert market.waiting == {"one"}
        market.ready.set()
        assert phases.opened.wait(timeout=2)
        future.result(timeout=3)
        assert market.waiting == set()
        assert result == [{"status": "stopped", "reason": "finished"}]
    finally:
        runtime.close()


def _wait(predicate) -> None:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + 2
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert predicate()
