from __future__ import annotations

import time
from decimal import Decimal
from enum import StrEnum
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr
from pydantic.alias_generators import to_camel
from pydantic.functional_validators import model_validator
from .models_shared import CamelModel, StrategyTargetMode

class BetaCampaignStatus(StrEnum):
    PLANNED = "planned"
    EXECUTING = "executing"
    STOPPING = "stopping"
    COMPLETED = "completed"
    STOPPED = "stopped"
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
    """Intentionally empty: sizing and timing always come from the binding."""


class BoundStrategyExecutionExecuteRequest(CamelModel):
    risk_acknowledged: bool
    confirmation: str = Field(min_length=1, max_length=200)


class BoundStrategyExecutionStopRequest(CamelModel):
    confirmation: str = Field(min_length=1, max_length=200)


class BoundStrategyExecutionReconcileRequest(CamelModel):
    confirmation: str = Field(min_length=1, max_length=240)


class BetaCampaignExecuteRequest(CamelModel):
    risk_acknowledged: bool
    confirmation: str = Field(min_length=1, max_length=200)


class BetaCampaignStopRequest(CamelModel):
    confirmation: str = Field(min_length=1, max_length=200)


class BetaCampaignReconcileRequest(CamelModel):
    confirmation: str = Field(min_length=1, max_length=240)


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
