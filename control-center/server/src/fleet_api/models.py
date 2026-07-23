from __future__ import annotations

import time
from decimal import Decimal
from enum import StrEnum
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr
from pydantic.alias_generators import to_camel
from pydantic.functional_validators import model_validator


class CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, serialize_by_alias=True, extra="forbid")


class TradingMode(StrEnum):
    DEMO = "demo"
    LIVE = "live"


class InstanceStatus(StrEnum):
    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"
    WARNING = "warning"
    ERROR = "error"


class ProxyType(StrEnum):
    NONE = "none"
    HTTP = "http"
    HTTPS = "https"
    SOCKS5 = "socks5"


class ProxyStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNCHECKED = "unchecked"


class LogLevel(StrEnum):
    INFO = "info"
    SUCCESS = "success"
    WARN = "warn"
    ERROR = "error"


class InstanceAction(StrEnum):
    START = "start"
    PAUSE = "pause"
    STOP = "stop"


class StrategyStage(StrEnum):
    IDLE = "idle"
    HOLDING = "holding"
    COOLDOWN = "cooldown"
    COMPLETE = "complete"


class StrategyTargetMode(StrEnum):
    INCREMENTAL = "incremental"
    LIFETIME = "lifetime"


class FundingPreflightStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    INSUFFICIENT = "insufficient"


class CredentialInput(CamelModel):
    api_key: SecretStr = Field(min_length=1)
    api_secret: SecretStr = Field(min_length=1)
    passphrase: SecretStr = Field(min_length=1)


class ProxyInput(CamelModel):
    type: ProxyType
    url: SecretStr | None = None

    @model_validator(mode="after")
    def validate_proxy_url(self) -> Self:
        value = self.url.get_secret_value().strip() if self.url is not None else ""
        if self.type is ProxyType.NONE:
            if value:
                raise ValueError("proxy URL must be empty when proxy type is none")
            return self
        if not value:
            raise ValueError("proxy URL is required")
        return self


class VolumeStrategyInput(CamelModel):
    name: str = Field(default="成交量策略", min_length=1, max_length=64)
    target_mode: StrategyTargetMode = StrategyTargetMode.INCREMENTAL
    target_volume_quote: Decimal = Field(gt=0, le=1_000_000_000_000, multiple_of=Decimal("0.01"))
    round_turnover_quote_min: Decimal = Field(gt=0, le=1_000_000_000, multiple_of=Decimal("0.01"))
    round_turnover_quote_max: Decimal = Field(gt=0, le=1_000_000_000, multiple_of=Decimal("0.01"))
    position_hold_min_seconds: int = Field(default=5, ge=0, le=2_592_000)
    position_hold_max_seconds: int = Field(default=15, ge=0, le=2_592_000)
    round_interval_min_seconds: int = Field(default=10, ge=0, le=2_592_000)
    round_interval_max_seconds: int = Field(default=30, ge=0, le=2_592_000)

    @model_validator(mode="after")
    def validate_ranges(self) -> Self:
        if self.round_turnover_quote_min > self.round_turnover_quote_max:
            raise ValueError("round turnover minimum cannot exceed maximum")
        if self.position_hold_min_seconds > self.position_hold_max_seconds:
            raise ValueError("position hold minimum cannot exceed maximum")
        if self.round_interval_min_seconds > self.round_interval_max_seconds:
            raise ValueError("round interval minimum cannot exceed maximum")
        return self


class VolumeStrategy(VolumeStrategyInput):
    id: str = Field(min_length=1, max_length=80)
    # Server-managed ownership boundary. Requests never accept this field.
    owner_user_id: str = Field(default="gg", min_length=1, max_length=48)
    # Each shared-strategy edit creates the next immutable audit version.
    # Existing SQLite payloads omit it and therefore deserialize as version 1.
    version: int = Field(default=1, ge=1)


class StrategyProgress(CamelModel):
    generated_volume_quote: Decimal = Field(default=Decimal(0), ge=0)
    started_at_ms: int | None = Field(default=None, gt=0)
    stage: StrategyStage = StrategyStage.IDLE
    next_action_at_ms: int | None = Field(default=None, gt=0)
    active_cycle_id: str | None = Field(default=None, max_length=80)
    last_eth_ratio: Decimal | None = Field(default=None, gt=0)
    last_allocation_version: str | None = Field(default=None, max_length=80)
    system_pause_reason: str | None = Field(default=None, max_length=96)


def default_volume_strategy() -> VolumeStrategy:
    return VolumeStrategy(
        id="strategy-default",
        name="默认成交量策略",
        target_volume_quote=Decimal("4000"),
        round_turnover_quote_min=Decimal("40"),
        round_turnover_quote_max=Decimal("40"),
        position_hold_min_seconds=0,
        position_hold_max_seconds=0,
        round_interval_min_seconds=0,
        round_interval_max_seconds=0,
    )


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
    updated_at: str = "尚未同步"
    unread_logs: int = 0

    @model_validator(mode="after")
    def validate_strategy_projection(self) -> Self:
        if not self.strategy_id:
            self.strategy_id = self.strategy.id
        elif self.strategy_id != self.strategy.id:
            raise ValueError("account strategy projection does not match strategy id")
        return self


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
    schema_version: int = 3
    instance_id: str
    session_id: str | None = None
    execution_id: str | None = None
    executor_generation: str
    status: str
    phase: str
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


class BetaMarketSnapshot(CamelModel):
    schema_version: str
    strategy: str
    status: str
    upstream_usable: bool
    reason_codes: list[str]
    final_beta: Decimal
    btc_long_ratio: Decimal
    eth_short_ratio: Decimal
    btc_long_weight: Decimal
    eth_short_weight: Decimal
    confidence: Decimal
    confidence_threshold: Decimal
    source: str
    as_of_ms: int
    generated_at_ms: int
    age_ms: Decimal
    max_age_ms: Decimal


class BetaSourceSettings(CamelModel):
    """Non-secret, shared Beta allocation source configuration."""

    url: str = Field(min_length=1, max_length=2_048)
    timeout_seconds: float = Field(gt=0, le=60)
    refresh_interval_seconds: float = Field(gt=0, le=3_600)
    background_refresh_enabled: bool
    updated_at_ms: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_url(self) -> Self:
        parsed = urlsplit(self.url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Beta source URL must be an absolute HTTP(S) URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("Beta source URL must not contain credentials")
        return self


class BetaSourceSettingsUpdate(CamelModel):
    url: str = Field(min_length=1, max_length=2_048)
    timeout_seconds: float = Field(gt=0, le=60)
    refresh_interval_seconds: float = Field(gt=0, le=3_600)
    background_refresh_enabled: bool

    @model_validator(mode="after")
    def validate_url(self) -> Self:
        BetaSourceSettings(
            url=self.url,
            timeout_seconds=self.timeout_seconds,
            refresh_interval_seconds=self.refresh_interval_seconds,
            background_refresh_enabled=self.background_refresh_enabled,
            updated_at_ms=1,
        )
        return self


class VolumeSessionCreateRequest(CamelModel):
    session_id: str = Field(min_length=1, max_length=128)
    target_quote_volume: Decimal = Field(gt=0, max_digits=30, decimal_places=18)
    started_at_ms: int | None = Field(default=None, gt=0)
    maker_only_required: bool = False


class VolumeSessionResponse(CamelModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, serialize_by_alias=True, extra="ignore")
    session_id: str
    account_id: str
    mode: str
    started_at_ms: int
    finished_at_ms: int | None = None
    strategy_id: str | None = None
    strategy_name: str | None = None
    strategy_version: int | None = None
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


class VolumeSessionReconcileRequest(CamelModel):
    fills: list[dict[str, object]] = Field(default_factory=list)


class StrategyRunSummary(CamelModel):
    session_id: str
    strategy_id: str | None = None
    strategy_name: str | None = None
    strategy_version: int | None = None
    target_mode: StrategyTargetMode
    started_at_ms: int
    finished_at_ms: int | None = None
    status: str
    result: str | None = None
    result_reason: str | None = None
    strategy_target_quote_volume: Decimal
    execution_target_quote_volume: Decimal
    verified_quote_volume: Decimal
    remaining_quote_volume: Decimal
    baseline_lifetime_quote_volume: Decimal
    final_lifetime_quote_volume: Decimal | None = None
    starting_available_balance_quote: Decimal | None = None
    ending_available_balance_quote: Decimal | None = None
    available_balance_change_quote: Decimal | None = None
    source_complete: bool
    stale: bool
    reconciliation_required: bool


class StrategyRunPage(CamelModel):
    items: list[StrategyRunSummary]
    next_cursor: str | None = None


class HealthResponse(CamelModel):
    status: str = "ok"
    adapter: str
    storage: str
    live_trading_enabled: bool = False
    execution_enabled: bool = False
    live_campaigns_enabled: bool = False
    # Public capability for the confirmation-gated, bound-strategy Live path.
    # Keep `execution_enabled` reserved for the legacy Mock runtime.
    bound_strategy_execution_enabled: bool = False
    # Actual in-process campaigns, distinct from the configured thread-pool
    # capacity exposed below. Release migration uses this to avoid two owners.
    live_campaign_active_worker_count: int = Field(default=0, ge=0)
    live_campaign_worker_count: int = Field(default=0, ge=0)
    api_release_id: str | None = None
    executor_connected: bool = True
    executor_generation: str | None = None
    monitor_projection_lag: int = Field(default=0, ge=0)
    monitor_ledger_lag: int = Field(default=0, ge=0)
    monitor_sse_subscriber_count: int = Field(default=0, ge=0)
    monitor_sse_reset_count: int = Field(default=0, ge=0)
    monitor_transaction_failure_count: int = Field(default=0, ge=0)
    monitor_last_event_at_ms: int | None = None


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
