from __future__ import annotations

import time
from decimal import Decimal
from enum import StrEnum
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr
from pydantic.alias_generators import to_camel
from pydantic.functional_validators import model_validator
from .models_shared import (
    CamelModel,
    FundingPreflightStatus,
    InstanceStatus,
    ProxyStatus,
    ProxyType,
    StrategyDirection,
    StrategyTargetMode,
    TradingMode,
)
from .models_strategy import CredentialInput, ProxyInput, StrategyProgress, VolumeStrategy, default_volume_strategy

class CreateInstanceRequest(CamelModel):
    name: str = Field(min_length=1, max_length=64)
    account_tag: str = Field(default="未分组", max_length=64)
    mode: TradingMode = TradingMode.DEMO
    cycle_target: int = Field(default=100, ge=1, le=1_000_000)
    mock_cycle_total_quote: Decimal | None = Field(default=None, gt=0, le=1_000_000)
    strategy_id: str | None = Field(default=None, min_length=1, max_length=80)
    history_start_at_ms: int | None = Field(default=None, gt=0)
    credentials: CredentialInput
    proxy: ProxyInput

    @model_validator(mode="after")
    def validate_history_start(self) -> Self:
        if self.history_start_at_ms is not None and self.history_start_at_ms > int(time.time() * 1000):
            raise ValueError("history start cannot be in the future")
        return self


class UpdateInstanceRequest(CamelModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    account_tag: str | None = Field(default=None, max_length=64)
    cycle_target: int | None = Field(default=None, ge=1, le=1_000_000)
    mock_cycle_total_quote: Decimal | None = Field(default=None, gt=0, le=1_000_000)
    strategy_id: str | None = Field(default=None, min_length=1, max_length=80)
    history_start_at_ms: int | None = Field(default=None, gt=0)
    credentials: CredentialInput | None = None
    proxy: ProxyInput | None = None

    @model_validator(mode="after")
    def require_change(self) -> Self:
        if (
            self.name is None
            and self.account_tag is None
            and self.cycle_target is None
            and self.mock_cycle_total_quote is None
            and self.strategy_id is None
            and "history_start_at_ms" not in self.model_fields_set
            and self.credentials is None
            and self.proxy is None
        ):
            raise ValueError("at least one account field is required")
        if self.history_start_at_ms is not None and self.history_start_at_ms > int(time.time() * 1000):
            raise ValueError("history start cannot be in the future")
        return self


class ProxySnapshot(CamelModel):
    type: ProxyType
    host: str
    location: str = "待检测"
    latency_ms: int | None = None
    status: ProxyStatus = ProxyStatus.UNCHECKED


class WalletSnapshot(CamelModel):
    equity: float = 0
    available: float = 0
    unrealized_pnl: float = 0


class FundingPreflightSnapshot(CamelModel):
    status: FundingPreflightStatus = FundingPreflightStatus.PENDING
    available_quote: Decimal | None = Field(default=None, ge=0)
    opening_notional_quote: Decimal = Field(default=Decimal(0), ge=0)
    required_leverage: int | None = Field(default=None, ge=1)
    planned_leverage: int | None = Field(default=None, ge=1, le=99)
    max_leverage: int = Field(default=99, ge=1, le=99)
    safety_buffer: Decimal = Field(default=Decimal("1.20"), gt=1)
    max_supported_turnover_quote: Decimal | None = Field(default=None, ge=0)
    reason: str = Field(default="wallet_not_synchronized", max_length=80)


class SessionVolumeProjection(CamelModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, serialize_by_alias=True, extra="ignore")

    @model_validator(mode="before")
    @classmethod
    def upgrade_legacy_projection(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        strategy_target = value.get("strategyTargetQuoteVolume", value.get("strategy_target_quote_volume"))
        if strategy_target is not None:
            return value
        target = value.get("targetQuoteVolume", value.get("target_quote_volume"))
        if target is None:
            return value
        return {**value, "strategyTargetQuoteVolume": target}

    session_id: str
    account_id: str
    mode: str
    started_at_ms: int
    finished_at_ms: int | None = None
    strategy_id: str | None = None
    strategy_name: str | None = None
    strategy_version: int | None = None
    direction: StrategyDirection = StrategyDirection.BTC_LONG_ETH_SHORT
    target_mode: StrategyTargetMode = StrategyTargetMode.INCREMENTAL
    strategy_target_quote_volume: Decimal
    baseline_lifetime_quote_volume: Decimal = Decimal(0)
    final_lifetime_quote_volume: Decimal | None = None
    result: str | None = None
    result_reason: str | None = None
    target_quote_volume: Decimal
    verified_quote_volume: Decimal
    remaining_quote_volume: Decimal
    status: str
    audit_status: Literal["verified", "pending", "discrepant"] = "pending"
    fill_count: int
    opening_quote_volume: Decimal
    closing_quote_volume: Decimal
    maker_quote_volume: Decimal
    taker_quote_volume: Decimal
    unknown_liquidity_quote_volume: Decimal
    last_sync_at_ms: int | None
    last_reconciliation_at_ms: int | None
    source_complete: bool
    stale: bool
    reconciliation_required: bool
    discrepancy_quote_volume: Decimal
    retry_allowed: bool = False


class HistorySyncProjection(CamelModel):
    """Safe, durable account-history synchronization state for read-only views."""

    state: Literal[
        "not_requested",
        "initial_baseline_queued",
        "initial_baseline_running",
        "initial_baseline_pending",
        "incremental_queued",
        "syncing",
        "fresh",
        "stale",
    ] = "not_requested"
    reason: str | None = Field(default=None, max_length=40)
    initial_baseline_state: Literal["not_requested", "queued", "running", "complete", "pending"] = "not_requested"
    pending: bool = False
    source_complete: bool = False
    stale: bool = False
    last_success_at_ms: int | None = Field(default=None, ge=0)
    next_sync_at_ms: int | None = Field(default=None, ge=0)
    high_watermark_ms: int | None = Field(default=None, ge=0)


class VolumeSnapshot(CamelModel):
    lifetime: float = 0
    today: float = 0
    complete: bool = False
    session: SessionVolumeProjection | None = Field(default=None, exclude_if=lambda value: value is None)
    active_session: SessionVolumeProjection | None = Field(default=None, exclude_if=lambda value: value is None)
    last_run: SessionVolumeProjection | None = Field(default=None, exclude_if=lambda value: value is None)
    lifetime_source_complete: bool | None = Field(default=None, exclude_if=lambda value: value is None)
    strategy_target_quote_volume: Decimal | None = Field(default=None, exclude_if=lambda value: value is None)
    strategy_verified_quote_volume: Decimal | None = Field(default=None, exclude_if=lambda value: value is None)
    strategy_remaining_quote_volume: Decimal | None = Field(default=None, exclude_if=lambda value: value is None)
    strategy_target_reached: bool | None = Field(default=None, exclude_if=lambda value: value is None)
    # The account table reads this server-side projection only.  "execution_journal"
    # means the worker has reconciled concrete fills but the wider SQLite fill
    # ledger has not yet completed its independent sync/reconciliation pass.
    strategy_progress_source: Literal["ledger", "execution_journal", "pending"] | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    strategy_progress_updated_at_ms: int | None = Field(default=None, exclude_if=lambda value: value is None)
    history_sync: HistorySyncProjection | None = Field(default=None, exclude_if=lambda value: value is None)


class ExposureSnapshot(CamelModel):
    btc_long: float = 0
    eth_short: float = 0


class CycleSnapshot(CamelModel):
    completed: int = Field(default=0, ge=0)
    target: int = Field(default=100, ge=1, le=1_000_000)
    next_action_at: str | None = None


class RuntimeHealthSnapshot(CamelModel):
    last_poll_started_at_ms: int | None = None
    last_poll_succeeded_at_ms: int | None = None
    last_poll_failed_at_ms: int | None = None
    last_poll_duration_ms: int | None = Field(default=None, ge=0)
    consecutive_failures: int = Field(default=0, ge=0)
    last_error_type: str | None = None
    # A verified stop is a safety checkpoint, not a telemetry health signal.
    last_stop_verified_at_ms: int | None = None


class ExecutionLifecycleSnapshot(CamelModel):
    state: Literal["idle", "preparing", "running", "stopping", "recovering", "cleanup_required"] = "idle"
    primary_action: Literal["start", "stop", "wait", "cleanup"] = "start"
    execution_id: str | None = None
    session_id: str | None = None
    reason_code: str | None = None
    position_count: int = Field(default=0, ge=0)
    regular_order_count: int = Field(default=0, ge=0)
    trigger_order_count: int = Field(default=0, ge=0)


class SchedulerMetrics(CamelModel):
    max_parallel_polls: int = Field(ge=1)
    active_polls: int = Field(default=0, ge=0)
    max_observed_parallelism: int = Field(default=0, ge=0)
    poll_rounds: int = Field(default=0, ge=0)
    accounts_polled: int = Field(default=0, ge=0)
    successful_polls: int = Field(default=0, ge=0)
    failed_polls: int = Field(default=0, ge=0)
    last_round_account_count: int = Field(default=0, ge=0)
    last_round_succeeded: int = Field(default=0, ge=0)
    last_round_failed: int = Field(default=0, ge=0)
    last_round_started_at_ms: int | None = None
    last_round_completed_at_ms: int | None = None
    last_round_duration_ms: int | None = Field(default=None, ge=0)


class AccountInstance(CamelModel):
    id: str
    # Server-managed and returned only to the owning local user. This lets
    # independent executor workers retain the correct persistent identity.
    owner_user_id: str = Field(default="gg", min_length=1, max_length=48)
    name: str
    account_tag: str
    api_key_tail: str
    mode: TradingMode
    status: InstanceStatus
    phase: str
    proxy: ProxySnapshot
    wallet: WalletSnapshot = Field(default_factory=WalletSnapshot)
    funding_preflight: FundingPreflightSnapshot = Field(default_factory=FundingPreflightSnapshot)
    volume: VolumeSnapshot = Field(default_factory=VolumeSnapshot)
    exposure: ExposureSnapshot = Field(default_factory=ExposureSnapshot)
    cycle: CycleSnapshot = Field(default_factory=CycleSnapshot)
    strategy_id: str = Field(default="", max_length=80)
    strategy: VolumeStrategy = Field(default_factory=default_volume_strategy)
    strategy_progress: StrategyProgress = Field(default_factory=StrategyProgress)
    mock_cycle_total_quote: Decimal | None = Field(default=None, gt=0, le=1_000_000)
    history_start_at_ms: int | None = Field(default=None, gt=0)
    runtime: RuntimeHealthSnapshot = Field(default_factory=RuntimeHealthSnapshot)
    execution_lifecycle: ExecutionLifecycleSnapshot = Field(default_factory=ExecutionLifecycleSnapshot)
    updated_at: str = "尚未同步"
    unread_logs: int = 0

    @model_validator(mode="after")
    def validate_strategy_projection(self) -> Self:
        if not self.strategy_id:
            self.strategy_id = self.strategy.id
        elif self.strategy_id != self.strategy.id:
            raise ValueError("account strategy projection does not match strategy id")
        return self
