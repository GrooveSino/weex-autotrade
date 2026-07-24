"""Coroutine program that gives one Campaign a nonblocking lifecycle."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from weex_cli.beta_campaign import BetaVolumeCampaign

from .async_execution_orchestrator import ExecutionActor
from .campaign_actor_models import CampaignActorContext, CloseCycle, OpenCycle
from .campaign_actor_phases import CampaignActorPhases

ResultSink = Callable[[dict[str, Any]], None]
FailureSink = Callable[[Exception], None]


class CampaignActorProgram:
    """Use phase slots for I/O and actor timers for holding and round gaps."""

    def __init__(
        self,
        campaign: BetaVolumeCampaign,
        phases: CampaignActorPhases,
        *,
        proxy_key: str,
        on_result: ResultSink,
        on_failure: FailureSink,
    ) -> None:
        self._campaign = campaign
        self._phases = phases
        self._proxy_key = proxy_key
        self._on_result = on_result
        self._on_failure = on_failure

    async def __call__(self, actor: ExecutionActor) -> None:
        context: CampaignActorContext | None = None
        opened: OpenCycle | None = None
        try:
            actor.transition("preparing")
            context = await actor.run_blocking(self._phases.prepare, self._campaign)
            while True:
                if actor.stop_event.is_set():
                    await self._finish_stopped(actor, context, opened)
                    return
                if context.round_number > self._maximum_rounds(context):
                    result = await actor.run_blocking(
                        self._phases.finish,
                        self._campaign,
                        context,
                        status="stopped",
                        reason="maximum_rounds_exceeded",
                    )
                    await self._deliver(actor, result, "stopped")
                    return
                opened = await self._open(actor, context)
                if opened is None:
                    await self._finish_stopped(actor, context, None)
                    return
                if opened.hold_seconds > 0:
                    hold_started_at_ms = opened.hold_started_at_ms or opened.started_at_ms
                    deadline = hold_started_at_ms + int(opened.hold_seconds * 1_000)
                    if not await actor.sleep_until(deadline, phase="holding", reason="hold"):
                        await self._finish_stopped(actor, context, opened)
                        return
                outcome = await self._close(actor, opened)
                opened = None
                if outcome is None:
                    await self._finish_stopped(actor, context, None)
                    return
                if await self._finish_outcome(actor, context, outcome):
                    return
                if outcome.round_gap_seconds > 0:
                    deadline = _now_ms() + int(outcome.round_gap_seconds * 1_000)
                    if not await actor.sleep_until(deadline, phase="holding", reason="round_gap"):
                        await self._finish_stopped(actor, context, None)
                        return
        except Exception as exc:  # Durable submission classification stays in the manager callback.
            await actor.run_blocking(self._on_failure, exc)
            actor.transition("recovering", reason=f"phase_exception:{type(exc).__name__.lower()}")

    async def _open(self, actor: ExecutionActor, context: CampaignActorContext) -> OpenCycle | None:
        reservation = await actor.wait_for_normal_phase(
            "open",
            proxy_key=self._proxy_key,
            round_number=context.round_number,
        )
        if reservation is None:
            return None
        try:
            return await actor.run_blocking(self._phases.open, self._campaign, context)
        finally:
            actor.finish_normal_phase(reservation)

    async def _close(self, actor: ExecutionActor, opened: OpenCycle) -> CloseCycle | None:
        reservation = await actor.wait_for_normal_phase(
            "close",
            proxy_key=self._proxy_key,
            round_number=opened.context.round_number,
        )
        if reservation is None:
            return None
        try:
            return await actor.run_blocking(self._phases.close, self._campaign, opened)
        finally:
            actor.finish_normal_phase(reservation)

    async def _finish_outcome(
        self,
        actor: ExecutionActor,
        context: CampaignActorContext,
        outcome: CloseCycle,
    ) -> bool:
        if outcome.child_result is not None:
            result = await actor.run_blocking(
                self._phases.finish,
                self._campaign,
                context,
                status=str(outcome.child_result.get("status") or "completed"),
                reason=str(outcome.child_result.get("reason") or "target_verified_complete"),
                child_result=outcome.child_result,
            )
            await self._deliver(actor, result, _actor_terminal_phase(result))
            return True
        if outcome.uncertain_reason is not None:
            result = await actor.run_blocking(
                self._phases.finish,
                self._campaign,
                context,
                status="uncertain",
                reason=outcome.uncertain_reason,
            )
            await self._deliver(actor, result, "recovering")
            return True
        if outcome.stopped_reason is not None:
            result = await actor.run_blocking(
                self._phases.finish,
                self._campaign,
                context,
                status="stopped",
                reason=outcome.stopped_reason,
            )
            await self._deliver(actor, result, "stopped")
            return True
        return False

    async def _finish_stopped(
        self,
        actor: ExecutionActor,
        context: CampaignActorContext,
        opened: OpenCycle | None,
    ) -> None:
        if opened is not None:
            safe_result = await actor.run_blocking(self._phases.safe_stop, opened, emergency=True)
            status = str(safe_result.get("status") or "stopped")
            reason = str(safe_result.get("reason") or "stop_requested")
            result = await actor.run_blocking(
                self._phases.finish,
                self._campaign,
                context,
                status=status,
                reason=reason,
                child_result=safe_result,
            )
        else:
            result = await actor.run_blocking(
                self._phases.finish,
                self._campaign,
                context,
                status="stopped",
                reason="stop_requested",
            )
        await self._deliver(actor, result, _actor_terminal_phase(result))

    async def _deliver(self, actor: ExecutionActor, result: dict[str, Any], phase: str) -> None:
        await actor.run_blocking(self._on_result, result)
        actor.transition(phase, reason=str(result.get("reason") or None))

    @staticmethod
    def _maximum_rounds(context: CampaignActorContext) -> int:
        return context.child.estimated_rounds * 3 + context.child.max_empty_rounds + 5


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _actor_terminal_phase(result: dict[str, Any]) -> str:
    status = str(result.get("status") or "stopped")
    return "recovering" if status == "uncertain" else "completed" if status == "completed" else "stopped"
