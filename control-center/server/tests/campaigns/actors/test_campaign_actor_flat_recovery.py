"""Regression coverage for failures after a verified flat cycle."""

from __future__ import annotations

import asyncio
import threading
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from fleet_api.campaigns.actors.campaign_actor_models import CampaignActorContext, CloseCycle, OpenCycle
from fleet_api.campaigns.actors.campaign_actor_program import CampaignActorProgram
from fleet_api.campaigns.core.campaign_log import campaign_event_log


class _Actor:
    execution_id = "execution-flat"

    def __init__(self) -> None:
        self.stop_event = threading.Event()
        self.transitions: list[tuple[str, str | None]] = []

    def transition(self, phase: str, *, reason: str | None = None) -> None:
        self.transitions.append((phase, reason))

    async def run_blocking(self, operation: Any, *args: object, **kwargs: object) -> object:
        return operation(*args, **kwargs)

    async def wait_for_normal_phase(self, *_args: object, **_kwargs: object) -> object:
        return object()

    def finish_normal_phase(self, _reservation: object) -> None:
        return


class _Market:
    enabled = False

    def fresh(self) -> bool:
        return True

    def set_waiting(self, _execution_id: str, _waiting: bool) -> None:
        return


class _Phases:
    def __init__(self) -> None:
        child = SimpleNamespace(estimated_rounds=10, max_empty_rounds=10)
        self.context = CampaignActorContext(child=child, run_number=1, execution_started_at_ms=1_000)
        self.plan_calls = 0
        self.close_calls = 0

    def prepare(self, _campaign: object) -> CampaignActorContext:
        return self.context

    def plan_open(self, _campaign: object, context: CampaignActorContext) -> OpenCycle:
        self.plan_calls += 1
        return OpenCycle(context, {}, None, None, {}, 400, {}, [], {}, 1_000, 0)  # type: ignore[arg-type]

    def check_open_conditions(self, _context: CampaignActorContext) -> None:
        return

    def execute_open(self, _campaign: object, _opened: OpenCycle) -> None:
        return

    def close(self, _campaign: object, opened: OpenCycle) -> CloseCycle:
        self.close_calls += 1
        if self.close_calls == 2:
            return CloseCycle(Decimal(0), {"status": "completed", "reason": "target_verified_complete"}, None, None, 0)
        opened.context.round_number += 1
        return CloseCycle(Decimal(0), None, None, None, 0)

    def finish(self, _campaign: object, _context: CampaignActorContext, **kwargs: object) -> dict[str, str]:
        return {"status": str(kwargs["status"]), "reason": str(kwargs["reason"])}


def test_flat_cycle_reenters_the_next_attempt_without_a_preview_reconfirmation() -> None:
    phases = _Phases()
    results: list[dict[str, Any]] = []
    failures: list[Exception] = []
    events: list[dict[str, Any]] = []
    program = CampaignActorProgram(
        SimpleNamespace(),  # type: ignore[arg-type]
        phases,  # type: ignore[arg-type]
        proxy_key="direct",
        shared_market=_Market(),  # type: ignore[arg-type]
        on_result=results.append,
        on_failure=failures.append,
        on_event=events.append,
    )
    actor = _Actor()

    asyncio.run(program(actor))  # type: ignore[arg-type]

    assert failures == []
    assert results == [{"status": "completed", "reason": "target_verified_complete"}]
    assert actor.transitions[-1] == ("completed", "target_verified_complete")
    assert phases.plan_calls == 2
    assert events == []


def test_condition_wait_has_an_automatic_chinese_next_action() -> None:
    rendered = campaign_event_log(
        {
            "name": "condition_waiting",
            "fields": {"condition": "beta_unavailable", "round": 2},
        }
    )

    assert rendered is not None
    assert "最新 Beta 暂不可用" in rendered[1]
    assert "自动读取最新 Beta" in rendered[1]
