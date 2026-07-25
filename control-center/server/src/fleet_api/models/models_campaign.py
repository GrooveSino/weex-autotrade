from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Literal, Self

from pydantic import Field
from pydantic.functional_validators import model_validator

from fleet_api.models.models_shared import CamelModel, StrategyDirection, StrategyTargetMode


class BetaCampaignStatus(StrEnum):
    PLANNED = "planned"
    EXECUTING = "executing"
    STOPPING = "stopping"
    COMPLETED = "completed"
    STOPPED = "stopped"
    RECOVERING = "recovering"
    UNCERTAIN = "uncertain"


class BetaCampaignPreviewRequest(CamelModel):
    target_quote: Decimal = Field(gt=0, le=1_000_000, multiple_of=Decimal("0.01"))
    cycle_volume: Decimal = Field(gt=0, le=1_000_000, multiple_of=Decimal("0.01"))
    hold_min_seconds: int = Field(default=300, ge=0, le=3600)
    hold_max_seconds: int = Field(default=420, ge=0, le=3600)
    round_gap_min_seconds: int = Field(default=300, ge=0, le=3600)
    round_gap_max_seconds: int = Field(default=420, ge=0, le=3600)

    @model_validator(mode="after")
    def validate_ranges(self) -> Self:
        if self.cycle_volume > self.target_quote:
            raise ValueError("cycle volume cannot exceed target quote")
        if self.hold_min_seconds > self.hold_max_seconds:
            raise ValueError("hold minimum cannot exceed hold maximum")
        if self.round_gap_min_seconds > self.round_gap_max_seconds:
            raise ValueError("round gap minimum cannot exceed round gap maximum")
        return self


class BoundStrategyExecutionPreviewRequest(CamelModel):
    direction: StrategyDirection = StrategyDirection.BTC_LONG_ETH_SHORT


class BoundStrategyExecutionExecuteRequest(CamelModel):
    risk_acknowledged: bool
    confirmation: str = Field(min_length=1, max_length=200)


class StrategyRunConfirmRequest(BoundStrategyExecutionExecuteRequest):
    execution_id: str = Field(min_length=1, max_length=128)


class StrategyRunCapacity(CamelModel):
    active_executions: int = Field(ge=0)
    max_active_executions: int = Field(ge=1)
    active_normal_phases: int = Field(ge=0)
    max_normal_phases: int = Field(ge=1)
    queued_normal_phases: int = Field(ge=0)
    revision: int = Field(ge=0)


class StrategyRunPhaseQueue(CamelModel):
    position: int | None = Field(default=None, ge=1)
    estimated_start_at_ms: int | None = Field(default=None, gt=0)
    proxy_limited: bool = False


class StrategyRunConfirmResponse(CamelModel):
    admission_state: Literal["admitted", "capacity_full"]
    execution_id: str
    execution: BetaCampaignView
    capacity: StrategyRunCapacity
    phase_queue: StrategyRunPhaseQueue | None = None


class BoundStrategyExecutionStopRequest(CamelModel):
    confirmation: str = Field(min_length=1, max_length=200)


class StrategyRunCleanupRequest(CamelModel):
    confirmation: str = Field(min_length=1, max_length=240)
    direction: StrategyDirection = StrategyDirection.BTC_LONG_ETH_SHORT
    command_id: str = Field(min_length=1, max_length=128)


class BetaCampaignExecuteRequest(CamelModel):
    risk_acknowledged: bool
    confirmation: str = Field(min_length=1, max_length=200)


class BetaCampaignStopRequest(CamelModel):
    confirmation: str = Field(min_length=1, max_length=200)


class BetaCampaignEvent(CamelModel):
    sequence: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=96)
    at_ms: int = Field(gt=0)
    phase: str | None = Field(default=None, max_length=96)
    run: int | None = Field(default=None, ge=1)
    child_plan_id: str | None = Field(default=None, max_length=96)
    status: str | None = Field(default=None, max_length=32)
    message: str | None = Field(default=None, max_length=240)
    fields: dict[str, object] = Field(default_factory=dict)


class BetaCampaignView(CamelModel):
    campaign_id: str
    instance_id: str
    status: BetaCampaignStatus
    schema_version: int
    strategy_id: str | None = None
    strategy_name: str | None = None
    strategy_version: int | None = Field(default=None, ge=1)
    strategy_snapshot: dict[str, object] | None = None
    session_id: str | None = None
    target_mode: StrategyTargetMode | None = None
    run_disposition: str | None = None
    strategy_target_quote_volume: Decimal | None = None
    execution_target_quote_volume: Decimal | None = None
    baseline_lifetime_quote_volume: Decimal | None = None
    direction: StrategyDirection = StrategyDirection.BTC_LONG_ETH_SHORT
    selected_target_quote_volume: Decimal | None = None
    leverage: str | int
    margin_mode: str
    dust_close_policy: dict[str, object] = Field(default_factory=dict)
    target_quote: Decimal
    round_turnover_quote_min: Decimal | None = None
    cycle_volume: Decimal
    authorized_max_quote: Decimal
    hold_min_seconds: int
    hold_max_seconds: int
    round_gap_min_seconds: int
    round_gap_max_seconds: int
    max_runs: int
    beta: Decimal
    beta_version: str
    beta_source: str
    beta_as_of_ms: int
    beta_age_ms: Decimal
    beta_max_age_ms: Decimal
    btc_long_weight: Decimal
    eth_short_weight: Decimal
    available_quote: Decimal | None = None
    required_leverage: int | None = None
    planned_leverage: int | None = None
    max_supported_turnover_quote: Decimal | None = None
    confirmation: str
    stop_confirmation: str
    reconciliation_confirmation: str | None = None
    reconciliation_required: bool = False
    retry_allowed: bool = False
    risk_acknowledged: bool = False
    current_run: int = 0
    generated_quote: Decimal = Decimal(0)
    remaining_quote: Decimal = Decimal(0)
    excess_quote: Decimal = Decimal(0)
    maker_quote: Decimal = Decimal(0)
    taker_quote: Decimal = Decimal(0)
    unknown_quote: Decimal = Decimal(0)
    btc_quote: Decimal = Decimal(0)
    eth_quote: Decimal = Decimal(0)
    fill_count: int = 0
    maker_count: int = 0
    taker_count: int = 0
    unknown_count: int = 0
    order_count: int = 0
    cancel_count: int = 0
    requote_count: int = 0
    phase: str = "planned"
    reason: str | None = None
    started_at_ms: int | None = None
    finished_at_ms: int | None = None
    elapsed_ms: int | None = None
    last_event: BetaCampaignEvent | None = None
    events: list[BetaCampaignEvent] = Field(default_factory=list)


class BetaCampaignPreview(BetaCampaignView):
    warnings: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)


class BlockingPosition(CamelModel):
    symbol: Literal["BTC", "ETH"]
    side: Literal["long", "short", "unknown"]
    quantity: Decimal = Field(ge=0)
    approximate_quote: Decimal = Field(ge=0)


class StrategyRunPrepareResponse(CamelModel):
    disposition: Literal[
        "ready",
        "running",
        "stopping",
        "recovering",
        "recovery_cleanup_required",
        "orders_cleanup_required",
        "position_blocked",
        "unavailable",
    ]
    preview: BetaCampaignPreview | None = None
    current: BetaCampaignView | None = None
    reason_code: str | None = None
    message: str | None = None
    position_count: int = Field(default=0, ge=0)
    regular_order_count: int = Field(default=0, ge=0)
    trigger_order_count: int = Field(default=0, ge=0)
    cleanup_confirmation: str | None = None
    blocking_positions: list[BlockingPosition] = Field(default_factory=list)
    allowed_actions: list[Literal["cancel_orders", "recheck", "safe_stop"]] = Field(default_factory=list)
    boundary_checked_at_ms: int | None = Field(default=None, gt=0)
