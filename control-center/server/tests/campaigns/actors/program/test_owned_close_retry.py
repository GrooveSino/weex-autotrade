from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from fleet_api.campaigns.actors.campaign_actor_models import CloseCycle, CycleCondition, OpenCycle
from fleet_api.campaigns.actors.campaign_actor_planning import retry_owned_close_condition
from fleet_api.campaigns.actors.campaign_actor_program import CampaignActorProgram
from fleet_api.execution.runtime.async_execution_orchestrator import AsyncExecutionOrchestrator
from fleet_api.execution.runtime.execution_capacity import ExecutionCapacity


class _Market:
    enabled = False

    def fresh(self) -> bool:
        return True

    def set_waiting(self, _execution_id: str, _waiting: bool) -> None:
        return


class _Phases:
    def __init__(self) -> None:
        self.opens = 0
        self.closes = 0
        self.safe_stops = 0
        self.opened: OpenCycle | None = None

    def prepare(self, _campaign: object):
        return SimpleNamespace(round_number=1, attempt_number=0, condition_attempt=0, condition_code=None)

    def check_open_conditions(self, _context: object) -> None:
        return

    def plan_open(self, _campaign: object, context: object) -> OpenCycle:
        self.opens += 1
        self.opened = OpenCycle(context, {}, None, None, {}, 400, {}, [], {}, 0, 0)  # type: ignore[arg-type]
        return self.opened

    def execute_open(self, _campaign: object, _opened: OpenCycle) -> None:
        return

    def close(self, _campaign: object, opened: OpenCycle) -> CloseCycle:
        self.closes += 1
        if self.closes == 1:
            return CloseCycle(
                Decimal(0),
                None,
                None,
                None,
                0,
                close_condition=CycleCondition("owned_close_maker_retry", "retry", "retry"),
            )
        assert opened is self.opened
        return CloseCycle(Decimal(2), {"status": "completed", "reason": "done"}, None, None, 0)

    def safe_stop(self, _opened: OpenCycle, *, emergency: bool) -> dict[str, str]:
        assert emergency
        self.safe_stops += 1
        return {"status": "stopped", "reason": "stop"}

    def finish(self, _campaign: object, _context: object, **kwargs: object) -> dict[str, str]:
        return {"status": str(kwargs["status"]), "reason": str(kwargs["reason"])}


def test_confirmed_owned_close_rejection_retries_close_without_reopening() -> None:
    capacity = ExecutionCapacity(
        max_active_executions=1,
        max_normal_phases=1,
        phase_start_rate_per_second=10_000,
        per_proxy_gap_seconds=0,
    )
    runtime = AsyncExecutionOrchestrator(capacity, normal_workers=1, emergency_workers=1)
    phases, results, events = _Phases(), [], []
    program = CampaignActorProgram(
        SimpleNamespace(),
        phases,
        proxy_key="proxy",
        shared_market=_Market(),
        on_result=results.append,
        on_failure=lambda error: (_ for _ in ()).throw(error),
        on_event=events.append,
    )
    assert capacity.admit("close-retry")
    try:
        runtime.start("close-retry", "account", program).result(timeout=3)
        assert phases.opens == 1
        assert phases.closes == 2
        assert phases.safe_stops == 0
        assert results == [{"status": "completed", "reason": "done"}]
        assert "condition_waiting" in [event["event"] for event in events]
        assert "owned_close_maker_retry_resumed" in [event["event"] for event in events]
    finally:
        runtime.close()


def test_only_confirmed_owned_close_rejections_are_retryable() -> None:
    retry = retry_owned_close_condition("post_only_rejected", flat=False, uncertain=False, owned=True)

    assert retry is not None
    assert retry.code == "owned_close_maker_retry"
    assert retry_owned_close_condition("post_only_rejected", flat=False, uncertain=True, owned=True) is None
    assert retry_owned_close_condition("post_only_rejected", flat=False, uncertain=False, owned=False) is None
    assert retry_owned_close_condition("submission_uncertain", flat=False, uncertain=False, owned=True) is None


def test_completed_result_projection_failure_cannot_reenter_safe_stop() -> None:
    capacity = ExecutionCapacity(
        max_active_executions=1,
        max_normal_phases=1,
        phase_start_rate_per_second=10_000,
        per_proxy_gap_seconds=0,
    )
    states, events = [], []
    runtime = AsyncExecutionOrchestrator(
        capacity,
        normal_workers=1,
        emergency_workers=1,
        state_sink=states.append,
    )
    phases = _Phases()

    def reject_projection(_result: object) -> None:
        raise TypeError("Decimal is not JSON serializable")

    program = CampaignActorProgram(
        SimpleNamespace(),
        phases,
        proxy_key="proxy",
        shared_market=_Market(),
        on_result=reject_projection,
        on_failure=lambda error: (_ for _ in ()).throw(error),
        on_event=events.append,
    )
    assert capacity.admit("projection-failure")
    try:
        runtime.start("projection-failure", "account", program).result(timeout=3)
        assert phases.safe_stops == 0
        assert states[-1].phase == "completed"
        assert events[-1]["event"] == "terminal_result_projection_failed"
    finally:
        runtime.close()
