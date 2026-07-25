"""Regression coverage for failures after a verified flat cycle."""

from __future__ import annotations

import asyncio
import threading
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from weex_cli.errors import SafetyError

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

    def prepare(self, _campaign: object) -> CampaignActorContext:
        return self.context

    def plan_open(self, _campaign: object, context: CampaignActorContext) -> OpenCycle:
        self.plan_calls += 1
        if self.plan_calls == 2:
            raise SafetyError("Beta moved more than 5% since planning")
        return OpenCycle(context, {}, None, None, {}, 400, {}, [], {}, 1_000, 0)  # type: ignore[arg-type]

    def execute_open(self, _campaign: object, _opened: OpenCycle) -> None:
        return

    def close(self, _campaign: object, opened: OpenCycle) -> CloseCycle:
        opened.context.round_number += 1
        return CloseCycle(Decimal(0), None, None, None, 0)

    def finish(self, _campaign: object, _context: CampaignActorContext, **kwargs: object) -> dict[str, str]:
        return {"status": str(kwargs["status"]), "reason": str(kwargs["reason"])}


def test_beta_change_after_flat_cycle_ends_without_recovery_dead_end() -> None:
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
    assert results == [{"status": "stopped", "reason": "worker_safety:beta_changed_since_preview"}]
    assert actor.transitions[-1] == ("stopped", "worker_safety:beta_changed_since_preview")
    assert events == [
        {
            "event": "next_cycle_preflight_rejected",
            "round": 2,
            "reason": "worker_safety:beta_changed_since_preview",
        }
    ]


def test_flat_cycle_beta_change_has_a_clear_restart_instruction() -> None:
    rendered = campaign_event_log(
        {
            "name": "next_cycle_preflight_rejected",
            "fields": {"reason": "worker_safety:beta_changed_since_preview", "round": 2},
        }
    )

    assert rendered is not None
    assert "未提交新的订单" in rendered[1]
    assert "重新确认新的策略预览" in rendered[1]
