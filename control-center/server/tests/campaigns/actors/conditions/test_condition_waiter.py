from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

from fleet_api.campaigns.actors.campaign_actor_models import CampaignActorContext, CycleCondition
from fleet_api.campaigns.actors.conditions.condition_waiter import (
    resume_condition_wait,
    wait_after_cycle_condition,
    wait_for_open_conditions,
)


class _Actor:
    execution_id = "condition-wait"

    def __init__(self) -> None:
        self.stop_event = threading.Event()
        self.sleeps: list[tuple[int, str]] = []
        self.transitions: list[tuple[str, str | None]] = []

    async def sleep_until(self, deadline_at_ms: int, *, phase: str, reason: str) -> bool:
        self.sleeps.append((deadline_at_ms, reason))
        return True

    def transition(self, phase: str, *, reason: str | None = None) -> None:
        self.transitions.append((phase, reason))


class _Market:
    enabled = False

    def fresh(self) -> bool:
        return True

    def set_waiting(self, _execution_id: str, _waiting: bool) -> None:
        return


def _context() -> CampaignActorContext:
    return CampaignActorContext(
        child=SimpleNamespace(),
        run_number=1,
        execution_started_at_ms=1,
    )


def test_sizing_condition_backoff_persists_until_a_new_plan_is_frozen(monkeypatch) -> None:
    actor, context, events = _Actor(), _context(), []
    condition = CycleCondition("minimum_order_infeasible", "稍后重算", "等待最小量条件恢复")
    monkeypatch.setattr(
        "fleet_api.campaigns.actors.conditions.condition_waiter._now_ms",
        lambda: 10_000,
    )

    async def scenario() -> None:
        async def emit(event: dict[str, object]) -> None:
            events.append(event)

        assert await wait_after_cycle_condition(actor, context, condition, emit_event=emit)
        assert await wait_after_cycle_condition(actor, context, condition, emit_event=emit)
        assert await wait_for_open_conditions(
            actor,
            context,
            shared_market=_Market(),
            read_conditions=_ready,
            emit_event=emit,
        )
        await resume_condition_wait(context, emit_event=emit)

    asyncio.run(scenario())

    assert [event["condition_attempt"] for event in events[:2]] == [1, 2]
    assert actor.sleeps == [(11_000, "minimum_order_infeasible"), (12_000, "minimum_order_infeasible")]
    assert context.condition_attempt == 0
    assert context.condition_code is None
    assert events[-1]["event"] == "condition_wait_resumed"


async def _ready() -> None:
    return
