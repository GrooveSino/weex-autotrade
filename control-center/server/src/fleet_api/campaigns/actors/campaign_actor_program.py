"""Coroutine program that gives one Campaign a nonblocking lifecycle."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from weex_cli.beta_campaign import BetaVolumeCampaign

from fleet_api.campaigns.actors.campaign_actor_models import CampaignActorContext, CloseCycle, OpenCycle
from fleet_api.campaigns.actors.campaign_actor_phases import CampaignActorPhases
from fleet_api.execution.resources.market_data_hub import PublicMarketSnapshotService
from fleet_api.execution.runtime.async_execution_orchestrator import ExecutionActor

ResultSink = Callable[[dict[str, Any]], None]
FailureSink = Callable[[Exception], None]
EventSink = Callable[[dict[str, Any]], None]


class CampaignActorProgram:
    """Use phase slots for I/O and actor timers for holding and round gaps."""

    def __init__(
        self,
        campaign: BetaVolumeCampaign,
        phases: CampaignActorPhases,
        *,
        proxy_key: str,
        shared_market: PublicMarketSnapshotService,
        on_result: ResultSink,
        on_failure: FailureSink,
        on_event: EventSink,
    ) -> None:
        self._campaign = campaign
        self._phases = phases
        self._proxy_key = proxy_key
        self._shared_market = shared_market
        self._on_result = on_result
        self._on_failure = on_failure
        self._on_event = on_event

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
                if not await self._wait_for_market(actor):
                    await self._finish_stopped(actor, context, opened)
                    return
                reservation = await actor.wait_for_normal_phase(
                    "open",
                    proxy_key=self._proxy_key,
                    round_number=context.round_number,
                )
                if reservation is None:
                    await self._finish_stopped(actor, context, None)
                    return
                try:
                    opened = await actor.run_blocking(self._phases.plan_open, self._campaign, context)
                    await actor.run_blocking(self._phases.execute_open, self._campaign, opened)
                finally:
                    actor.finish_normal_phase(reservation)
                if opened.hold_seconds > 0:
                    hold_started_at_ms = opened.hold_started_at_ms or opened.started_at_ms
                    deadline = hold_started_at_ms + int(opened.hold_seconds * 1_000)
                    if not await actor.sleep_until(deadline, phase="holding", reason="hold"):
                        await self._finish_stopped(actor, context, opened)
                        return
                    await self._emit_event(
                        actor,
                        {
                            "event": "hold_completed",
                            "round": opened.context.round_number,
                            "seconds": opened.hold_seconds,
                        },
                    )
                outcome = await self._close(actor, opened)
                if outcome is None:
                    # A stop can arrive after both opening legs have been
                    # verified but before the normal close slot is admitted.
                    # Keep the cycle context so safe_stop can cancel orders
                    # and maker-close any real residual exposure.
                    await self._finish_stopped(actor, context, opened)
                    return
                opened = None
                if await self._finish_outcome(actor, context, outcome):
                    return
                if outcome.round_gap_seconds > 0:
                    completed_round = context.round_number - 1
                    gap_started_at_ms = outcome.round_gap_started_at_ms or _now_ms()
                    deadline = gap_started_at_ms + int(outcome.round_gap_seconds * 1_000)
                    if not await actor.sleep_until(deadline, phase="holding", reason="round_gap"):
                        await self._finish_stopped(actor, context, None)
                        return
                    await self._emit_event(
                        actor,
                        {
                            "event": "round_gap_completed",
                            "round": completed_round,
                            "seconds": outcome.round_gap_seconds,
                        },
                    )
        except Exception as exc:  # Durable submission classification stays in the manager callback.
            if context is not None and opened is not None:
                actor.transition("stopping", reason=f"phase_exception:{type(exc).__name__.lower()}")
                try:
                    await self._finish_stopped(
                        actor,
                        context,
                        opened,
                        fallback_reason=f"phase_exception:{type(exc).__name__.lower()}",
                    )
                except Exception as cleanup_error:
                    exc = cleanup_error
                else:
                    return
            await actor.run_blocking(self._on_failure, exc)
            actor.transition("recovering", reason=f"phase_exception:{type(exc).__name__.lower()}")

    async def _close(self, actor: ExecutionActor, opened: OpenCycle) -> CloseCycle | None:
        if not await self._wait_for_market(actor):
            return None
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

    async def _wait_for_market(self, actor: ExecutionActor) -> bool:
        """Wait before phase admission so stale public data cannot occupy a slot."""
        if not self._shared_market.enabled:
            return not actor.stop_event.is_set()
        self._shared_market.set_waiting(actor.execution_id, True)
        try:
            while not actor.stop_event.is_set():
                if self._shared_market.fresh():
                    actor.transition("preparing", reason="shared_market_ready")
                    return True
                actor.transition("market_waiting", reason="shared_market_recovering")
                await _sleep_shortly()
            return False
        finally:
            self._shared_market.set_waiting(actor.execution_id, False)

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
        *,
        fallback_reason: str = "stop_requested",
    ) -> None:
        if opened is not None:
            safe_result = await actor.run_blocking(self._phases.safe_stop, opened, emergency=True)
            status = str(safe_result.get("status") or "stopped")
            reason = str(safe_result.get("reason") or fallback_reason)
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
                reason=fallback_reason,
            )
        await self._deliver(actor, result, _actor_terminal_phase(result))

    async def _deliver(self, actor: ExecutionActor, result: dict[str, Any], phase: str) -> None:
        await actor.run_blocking(self._on_result, result)
        actor.transition(phase, reason=str(result.get("reason") or None))

    async def _emit_event(self, actor: ExecutionActor, event: dict[str, Any]) -> None:
        try:
            await actor.run_blocking(self._on_event, event)
        except Exception:
            # A monitor write cannot alter order execution. Later phase
            # boundaries still supersede the timer wait during replay.
            return

    @staticmethod
    def _maximum_rounds(context: CampaignActorContext) -> int:
        return context.child.estimated_rounds * 3 + context.child.max_empty_rounds + 5


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


async def _sleep_shortly() -> None:
    # Keep the event-loop responsive while the one shared stream recovers.
    import asyncio

    await asyncio.sleep(0.1)


def _actor_terminal_phase(result: dict[str, Any]) -> str:
    status = str(result.get("status") or "stopped")
    return "recovering" if status == "uncertain" else "completed" if status == "completed" else "stopped"
