from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from fleet_api.campaigns.actors.campaign_actor_models import CampaignActorContext, CloseCycle, OpenCycle
from fleet_api.campaigns.actors.campaign_actor_program import CampaignActorProgram
from fleet_api.campaigns.manager.helpers.actor_values import accounting_checkpoint, summary_from_checkpoint
from fleet_api.execution.runtime.async_execution_orchestrator import AsyncExecutionOrchestrator
from fleet_api.execution.runtime.execution_capacity import ExecutionCapacity


class _Market:
    enabled = False

    def fresh(self) -> bool:
        return True

    def set_waiting(self, _execution_id: str, _waiting: bool) -> None:
        return


def _context() -> CampaignActorContext:
    return CampaignActorContext(
        child=SimpleNamespace(estimated_rounds=1, max_empty_rounds=1),
        run_number=1,
        execution_started_at_ms=1_000,
    )


class _ResumePhases:
    prepared_for_resume = False

    def prepare(self, _campaign: object) -> CampaignActorContext:
        raise AssertionError("restart must not replay initial preview validation")

    def prepare_for_resume(self, _campaign: object) -> None:
        self.prepared_for_resume = True

    def check_open_conditions(self, _context: CampaignActorContext) -> None:
        return

    def plan_open(self, _campaign: object, context: CampaignActorContext) -> OpenCycle:
        return OpenCycle(context, {}, None, None, {}, 400, {}, [], {}, 1_000, 0)  # type: ignore[arg-type]

    def execute_open(self, _campaign: object, _opened: OpenCycle) -> None:
        return

    def close(self, _campaign: object, _opened: OpenCycle) -> CloseCycle:
        return CloseCycle(Decimal(0), {"status": "completed", "reason": "done"}, None, None, 0)

    def finish(self, _campaign: object, _context: CampaignActorContext, **_kwargs: object) -> dict[str, str]:
        return {"status": "stopped", "reason": "finished"}


def test_rehydrated_condition_wait_skips_initial_preview_validation() -> None:
    phases = _ResumePhases()
    results: list[dict[str, str]] = []
    events: list[dict[str, object]] = []
    capacity = ExecutionCapacity(
        max_active_executions=1,
        max_normal_phases=1,
        phase_start_rate_per_second=10_000,
        per_proxy_gap_seconds=0,
    )
    runtime = AsyncExecutionOrchestrator(capacity, normal_workers=1, emergency_workers=1)
    assert capacity.admit("restart-one")
    program = CampaignActorProgram(
        SimpleNamespace(),  # type: ignore[arg-type]
        phases,  # type: ignore[arg-type]
        proxy_key="proxy-a",
        shared_market=_Market(),  # type: ignore[arg-type]
        on_result=results.append,
        on_failure=lambda _error: None,
        on_event=events.append,
        resume_context=_context(),
    )
    future = runtime.start("restart-one", "account-one", program)
    try:
        future.result(timeout=3)
        assert phases.prepared_for_resume
        assert results == [{"status": "stopped", "reason": "finished"}]
        assert any(event["event"] == "condition_wait_rehydrated" for event in events)
    finally:
        runtime.close()


def test_restart_accounting_checkpoint_preserves_authoritative_completion_proof() -> None:
    checkpoint = accounting_checkpoint(
        [
            {
                "quote_volume": "173.75",
                "accounting_verified": True,
                "liquidity_policy_satisfied": True,
                "fill_count": 4,
                "maker_count": 4,
            }
        ]
    )

    restored = summary_from_checkpoint(checkpoint, Decimal("173.75"))

    assert restored[0]["quote_volume"] == "173.75"
    assert restored[0]["accounting_verified"] is True
    assert restored[0]["liquidity_policy_satisfied"] is True
    assert restored[0]["fill_count"] == 4
