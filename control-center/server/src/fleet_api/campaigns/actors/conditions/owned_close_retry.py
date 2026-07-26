"""Interruptible waits for a confirmed owned-position Maker re-quote."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from fleet_api.campaigns.actors.campaign_actor_models import CloseCycle, OpenCycle
from fleet_api.campaigns.actors.conditions.condition_waiter import wait_after_cycle_condition

CloseRunner = Callable[[Any, OpenCycle], Awaitable[CloseCycle | None]]
EventWriter = Callable[[dict[str, Any]], Awaitable[None]]


async def retry_owned_close(
    actor: Any,
    opened: OpenCycle,
    outcome: CloseCycle,
    *,
    close: CloseRunner,
    emit_event: EventWriter,
) -> CloseCycle | None:
    """Keep the current cycle open until a close outcome is no longer retryable."""
    while outcome.close_condition is not None:
        if not await wait_after_cycle_condition(actor, opened.context, outcome.close_condition, emit_event=emit_event):
            return None
        opened.context.condition_attempt = 0
        opened.context.condition_code = None
        await emit_event({"event": "owned_close_maker_retry_resumed", "round": opened.context.round_number})
        outcome = await close(actor, opened)
        if outcome is None:
            return None
    return outcome
