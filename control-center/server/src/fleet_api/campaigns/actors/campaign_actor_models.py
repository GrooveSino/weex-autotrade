"""Durable values passed between short-lived asynchronous Campaign phases."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from weex_cli.beta_campaign import BetaVolumeCampaign, LiveBetaVolumeCampaignService
from weex_cli.beta_volume import BetaVolumePlan, LiveBetaVolumeService, PairLegPlan


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


@dataclass(frozen=True, slots=True)
class CloseCycle:
    cycle_quote: Decimal
    child_result: dict[str, Any] | None
    stopped_reason: str | None
    uncertain_reason: str | None
    round_gap_seconds: float
    round_gap_started_at_ms: int | None = None


@dataclass(slots=True)
class CampaignPhaseEnvironment:
    campaign_service: LiveBetaVolumeCampaignService
    volume_service: LiveBetaVolumeService
    close: Callable[[], None]


EnvironmentFactory = Callable[[str], CampaignPhaseEnvironment]
CampaignResult = tuple[str, str, Decimal, list[dict[str, Any]]]
Campaign = BetaVolumeCampaign
BOUNDARY_COUNTS = ("active_position_count", "regular_order_count", "trigger_order_count")
