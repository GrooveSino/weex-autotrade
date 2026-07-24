"""Emergency actor used to converge a persisted execution-owned position."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .async_execution_orchestrator import ExecutionActor
from .campaign_actor_models import OpenCycle
from .campaign_actor_phases import CampaignActorPhases


class CampaignRecoveryProgram:
    def __init__(
        self,
        phases: CampaignActorPhases,
        opened: OpenCycle,
        *,
        on_result: Callable[[dict[str, Any]], None],
        on_failure: Callable[[Exception], None],
    ) -> None:
        self._phases = phases
        self._opened = opened
        self._on_result = on_result
        self._on_failure = on_failure

    async def __call__(self, actor: ExecutionActor) -> None:
        actor.transition("stopping", reason="recovery_safe_stop")
        try:
            result = await actor.run_blocking(self._phases.safe_stop, self._opened, emergency=True)
            await actor.run_blocking(self._on_result, result)
            status = str(result.get("status") or "stopped")
            actor.transition("recovering" if status == "uncertain" else "stopped", reason=str(result.get("reason")))
        except Exception as exc:  # the manager classifies the durable submission boundary
            await actor.run_blocking(self._on_failure, exc)
            actor.transition("recovering", reason=f"recovery_exception:{type(exc).__name__.lower()}")
