"""Durable values passed between short-lived asynchronous Campaign phases."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from weex_cli.control_api.campaigns import BetaVolumeCampaign, LiveBetaVolumeCampaignService
from weex_cli.control_api.volume import BetaVolumePlan, LiveBetaVolumeService, PairLegPlan


@dataclass(slots=True)
class CampaignActorContext:
    child: BetaVolumePlan
    run_number: int
    execution_started_at_ms: int
    round_number: int = 1
    child_total_quote: Decimal = Decimal(0)
    summaries: list[dict[str, Any]] = field(default_factory=list)
    cycles: list[dict[str, Any]] = field(default_factory=list)
    empty_rounds: int = 0
    attempt_number: int = 0
    condition_attempt: int = 0
    condition_code: str | None = None


@dataclass(frozen=True, slots=True)
class CycleCondition:
    """A retryable, read-only prerequisite for the next flat opening phase."""

    code: str
    detail: str
    action: str


class CycleConditionError(RuntimeError):
    """Stop before the exchange mutation boundary and wait for conditions."""

    def __init__(self, condition: CycleCondition) -> None:
        super().__init__(condition.code)
        self.condition = condition


@dataclass(slots=True)
class OpenCycle:
    context: CampaignActorContext
    preflight: Mapping[str, Any]
    btc_plan: PairLegPlan
    eth_plan: PairLegPlan
    sizing: Mapping[str, Any]
    selected_leverage: int
    leverage_state: dict[str, str]
    open_summaries: list[dict[str, Any]]
    lane_stops: dict[str, tuple[str, str]]
    started_at_ms: int
    hold_seconds: float
    # The hold countdown starts only after both opening legs reach the
    # verified target, not when the opening orders first begin.
    hold_started_at_ms: int | None = None
    # A new immutable plan is created for every opening attempt.  Older
    # records omit it, in which case the user-authorized child remains valid
    # for recovery compatibility only.
    execution_plan: BetaVolumePlan | None = None
    # Close-side Maker attempts can be re-quoted while this same cycle remains
    # live. Keep their summaries with the opening fills so accounting is
    # finalized exactly once when the pair becomes flat.
    close_summaries: list[dict[str, Any]] = field(default_factory=list)

    @property
    def plan(self) -> BetaVolumePlan:
        return self.execution_plan or self.context.child


def cycle_plan_from_ownership(ownership: Mapping[str, Any], fallback: BetaVolumePlan) -> BetaVolumePlan:
    """Use a persisted cycle snapshot when a safe-stop actor is reconstructed."""
    snapshot = ownership.get("cycle_plan")
    try:
        return BetaVolumePlan.from_dict(snapshot) if isinstance(snapshot, Mapping) else fallback
    except Exception:
        return fallback


@dataclass(frozen=True, slots=True)
class CloseCycle:
    cycle_quote: Decimal
    child_result: dict[str, Any] | None
    stopped_reason: str | None
    uncertain_reason: str | None
    round_gap_seconds: float
    round_gap_started_at_ms: int | None = None
    condition: CycleCondition | None = None
    close_condition: CycleCondition | None = None


@dataclass(slots=True)
class CampaignPhaseEnvironment:
    campaign_service: LiveBetaVolumeCampaignService
    volume_service: LiveBetaVolumeService
    close: Callable[[], None]


EnvironmentFactory = Callable[[str], CampaignPhaseEnvironment]
CampaignResult = tuple[str, str, Decimal, list[dict[str, Any]]]
Campaign = BetaVolumeCampaign
BOUNDARY_COUNTS = ("active_position_count", "regular_order_count", "trigger_order_count")


def _actor_terminal_phase(result: Mapping[str, Any]) -> str:
    status = str(result.get("status") or "stopped")
    return "recovering" if status == "uncertain" else "completed" if status == "completed" else "stopped"
