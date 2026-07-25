from __future__ import annotations

from decimal import Decimal
from typing import Literal, Self

from pydantic import Field
from pydantic.functional_validators import model_validator

from fleet_api.models.models_account import AccountInstance
from fleet_api.models.models_shared import CamelModel, LogLevel
from fleet_api.models.models_strategy import VolumeStrategy


class LogLine(CamelModel):
    id: str
    timestamp: str
    level: LogLevel
    message: str


class LogBatch(CamelModel):
    lines: list[LogLine]
    cursor: str | None
    reset: bool = False


class ActiveExecutionWait(CamelModel):
    key: str
    label: str
    updated_at_ms: int
    elapsed_ms: int = 0
    remaining_ms: int | None = None
    detail: str = ""
    symbol: str | None = None
    action: str | None = None
    started_at_ms: int | None = None
    deadline_at_ms: int | None = None


class ExecutionTimelineEntry(CamelModel):
    id: str
    sequence: int
    at_ms: int
    level: LogLevel
    event_name: str
    title: str
    detail: str = ""


class StrategyMonitorSnapshot(CamelModel):
    schema_version: int = 5
    instance_id: str
    session_id: str | None = None
    execution_id: str | None = None
    executor_generation: str
    status: str
    phase: str
    execution_state: str | None = None
    phase_queue_position: int | None = Field(default=None, ge=1)
    phase_queue_estimated_start_at_ms: int | None = Field(default=None, gt=0)
    phase_queue_proxy_limited: bool = False
    current_run: int = 0
    current_round: int = 0
    target_quote_volume: Decimal = Decimal(0)
    verified_quote_volume: Decimal = Decimal(0)
    ledger_verified_quote_volume: Decimal = Decimal(0)
    remaining_quote_volume: Decimal = Decimal(0)
    volume_source: Literal["ledger", "execution_journal", "pending"] = "pending"
    source_complete: bool = False
    stale: bool = True
    reconciliation_required: bool = False
    ledger_sync_state: Literal["idle", "queued", "syncing", "complete", "stale"] = "idle"
    audit_status: Literal["verified", "pending", "discrepant"] = "pending"
    recovery_state: str | None = None
    recovery_attempt: int = Field(default=0, ge=0)
    next_recovery_check_at_ms: int | None = Field(default=None, gt=0)
    boundary_state: Literal["flat", "owned_exposure", "external_exposure", "unknown"] = "unknown"
    btc_quote_volume: Decimal = Decimal(0)
    eth_quote_volume: Decimal = Decimal(0)
    maker_fill_count: int = 0
    taker_fill_count: int = 0
    unknown_fill_count: int = 0
    submissions: int = 0
    cancels: int = 0
    requotes: int = 0
    active_waits: list[ActiveExecutionWait] = Field(default_factory=list)
    timeline: list[ExecutionTimelineEntry] = Field(default_factory=list)
    projection_sequence: int = 0
    projection_version: int = 0
    ledger_revision: int = 0
    server_time_ms: int = 0
    updated_at_ms: int = 0
    freshness: Literal["current", "stale", "rebuilding"] = "current"
    stream_state: Literal["ready", "catching_up", "reset_required"] = "ready"
    cursor: str | None = None
    has_more: bool = False


class StrategyMonitorEvent(CamelModel):
    type: str
    cursor: str | None = None
    snapshot: StrategyMonitorSnapshot | None = None
    from_sequence: int | None = None
    to_sequence: int | None = None
    journal_sequence: int | None = None
    projection_sequence: int | None = None
    server_time_ms: int | None = None
    timeline: list[ExecutionTimelineEntry] = Field(default_factory=list)
    active_waits: list[ActiveExecutionWait] = Field(default_factory=list)


class ExecutionCycleView(CamelModel):
    cycle_id: str
    sequence: int
    status: Literal["planned", "opened", "completed", "rejected", "uncertain"]
    reason: str
    total_quote: str
    turnover_quote: str
    btc_long_quote: str
    eth_short_quote: str
    allocation_version: str
    position_hold_seconds: int
    round_interval_seconds: int
    sizing_mode: Literal["range_random", "residual_finish", "legacy_fixed"]
    strategy_id: str
    created_at_ms: int
    updated_at_ms: int
    reconciliation_required: bool
    retry_allowed: bool = False


class GlobalStopRequest(CamelModel):
    confirmation: str


class GlobalStopResult(CamelModel):
    stopped: int
    cancel_verified: int = Field(default=0, ge=0)
    cancel_failed: int = Field(default=0, ge=0)


class StrategyAssignmentRequest(CamelModel):
    instance_ids: list[str] = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def unique_instances(self) -> Self:
        if len(set(self.instance_ids)) != len(self.instance_ids):
            raise ValueError("instance ids must be unique")
        return self


class StrategyAssignmentResult(CamelModel):
    strategy: VolumeStrategy
    instances: list[AccountInstance]
