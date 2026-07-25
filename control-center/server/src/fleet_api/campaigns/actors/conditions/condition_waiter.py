"""Cancellable condition waits that never retain an order phase reservation."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from fleet_api.campaigns.actors.campaign_actor_models import (
    CampaignActorContext,
    CycleCondition,
    CycleConditionError,
)

ConditionReader = Callable[[], Awaitable[None]]
EventWriter = Callable[[dict[str, Any]], Awaitable[None]]

_BACKOFF_SECONDS = (1, 2, 5, 10, 30)
_READ_ONLY_CONDITIONS = {
    "account_read_retry",
    "beta_unavailable",
    "external_account_boundary",
    "insufficient_available_margin",
    "shared_market_unavailable",
}


async def wait_for_open_conditions(
    actor: Any,
    context: CampaignActorContext,
    *,
    shared_market: Any,
    read_conditions: ConditionReader,
    emit_event: EventWriter,
    resume_on_ready: bool = False,
) -> bool:
    """Return only when all read-only prerequisites are fresh or stop wins."""
    while not actor.stop_event.is_set():
        condition = await _next_condition(actor, shared_market, read_conditions)
        if condition is None:
            if resume_on_ready or context.condition_code in _READ_ONLY_CONDITIONS:
                await resume_condition_wait(context, emit_event=emit_event)
            actor.transition("preparing", reason="conditions_ready")
            return True
        deadline_at_ms = await _wait_for_condition(actor, context, condition, emit_event=emit_event)
        if not await actor.sleep_until(
            deadline_at_ms,
            phase="condition_waiting",
            reason=condition.code,
        ):
            return False
    return False


async def wait_after_cycle_condition(
    actor: Any,
    context: CampaignActorContext,
    condition: CycleCondition,
    *,
    emit_event: EventWriter,
) -> bool:
    """Back off a confirmed no-fill attempt before reading fresh conditions."""
    deadline_at_ms = await _wait_for_condition(actor, context, condition, emit_event=emit_event)
    return await actor.sleep_until(deadline_at_ms, phase="condition_waiting", reason=condition.code)


async def resume_condition_wait(context: CampaignActorContext, *, emit_event: EventWriter) -> None:
    """Clear a wait only after its prerequisite or fresh plan is confirmed."""
    if not context.condition_attempt:
        return
    await emit_event(
        {
            "event": "condition_wait_resumed",
            "round": context.round_number,
            "attempt": _event_attempt(context),
        }
    )
    context.condition_attempt = 0
    context.condition_code = None


async def _wait_for_condition(
    actor: Any,
    context: CampaignActorContext,
    condition: CycleCondition,
    *,
    emit_event: EventWriter,
) -> int:
    context.condition_attempt += 1
    context.condition_code = condition.code
    seconds = _BACKOFF_SECONDS[min(context.condition_attempt - 1, len(_BACKOFF_SECONDS) - 1)]
    deadline_at_ms = _now_ms() + seconds * 1_000
    await emit_event(
        {
            "event": "condition_waiting",
            "round": context.round_number,
            "attempt": _event_attempt(context),
            "condition_attempt": context.condition_attempt,
            "condition": condition.code,
            "detail": condition.detail,
            "action": condition.action,
            "next_check_ms": deadline_at_ms,
        }
    )
    return deadline_at_ms


async def wait_for_shared_market(
    actor: Any,
    context: CampaignActorContext,
    *,
    shared_market: Any,
    emit_event: EventWriter,
) -> bool:
    """Wait for shared Maker pricing without consuming a normal phase slot."""
    try:
        return await wait_for_open_conditions(
            actor,
            context,
            shared_market=shared_market,
            read_conditions=_ready,
            emit_event=emit_event,
            resume_on_ready=True,
        )
    finally:
        shared_market.set_waiting(actor.execution_id, False)


async def _next_condition(actor: Any, shared_market: Any, reader: ConditionReader) -> CycleCondition | None:
    if shared_market.enabled and not shared_market.fresh():
        shared_market.set_waiting(actor.execution_id, True)
        return CycleCondition(
            code="shared_market_unavailable",
            detail="共享 BTC/ETH 行情暂不可用，系统会自动恢复后继续",
            action="等待共享行情恢复",
        )
    shared_market.set_waiting(actor.execution_id, False)
    try:
        await reader()
    except CycleConditionError as exc:
        return exc.condition
    return None


def _now_ms() -> int:
    import time

    return time.time_ns() // 1_000_000


def _event_attempt(context: CampaignActorContext) -> int:
    """Use the latest frozen attempt when one exists, otherwise the first candidate."""
    return max(1, context.attempt_number)


async def _ready() -> None:
    return
