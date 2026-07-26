from __future__ import annotations

from decimal import Decimal
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import ConfigDict, Field
from pydantic.alias_generators import to_camel
from pydantic.functional_validators import model_validator

from fleet_api.models.models_shared import CamelModel, StrategyDirection, StrategyTargetMode


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


class AccountTradeVolumePeriod(CamelModel):
    """A bounded, account-scoped summary of actual filled quote volume."""

    lookback_days: Literal[1, 7, 30]
    start_at_ms: int = Field(ge=0)
    end_at_ms: int = Field(gt=0)
    total_quote_volume: Decimal = Field(ge=0)
    maker_quote_volume: Decimal = Field(ge=0)
    taker_quote_volume: Decimal = Field(ge=0)
    unknown_liquidity_quote_volume: Decimal = Field(ge=0)
    trade_count: int = Field(ge=0)
    complete: bool
    warnings: list[str] = Field(default_factory=list)


class AccountTradeVolumeProjection(CamelModel):
    """Committed account-level totals returned after a manual history import."""

    lifetime_quote_volume: Decimal = Field(ge=0)
    today_quote_volume: Decimal = Field(ge=0)
    source_complete: bool


class AccountTradeVolumeReportResponse(CamelModel):
    periods: tuple[AccountTradeVolumePeriod, ...]
    generated_at_ms: int = Field(gt=0)
    ledger_scanned_fill_count: int = Field(ge=0)
    ledger_inserted_fill_count: int = Field(ge=0)
    ledger_deduplicated_fill_count: int = Field(ge=0)
    ledger_lifetime_quote_volume: Decimal = Field(ge=0)
    ledger_today_quote_volume: Decimal = Field(ge=0)
    ledger_source_complete: bool
    account_volume: AccountTradeVolumeProjection


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


class StrategyRunSummary(CamelModel):
    session_id: str
    strategy_id: str | None = None
    strategy_name: str | None = None
    strategy_version: int | None = None
    direction: StrategyDirection = StrategyDirection.BTC_LONG_ETH_SHORT
    target_mode: StrategyTargetMode
    started_at_ms: int
    finished_at_ms: int | None = None
    status: str
    audit_status: Literal["verified", "pending", "discrepant"] = "pending"
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
    active_execution_capacity: int = Field(default=0, ge=0)
    max_execution_capacity: int = Field(default=0, ge=0)
    active_normal_phase_capacity: int = Field(default=0, ge=0)
    max_normal_phase_capacity: int = Field(default=0, ge=0)
    queued_normal_phase_count: int = Field(default=0, ge=0)
    capacity_revision: int = Field(default=0, ge=0)
    active_normal_io: int = Field(default=0, ge=0)
    max_normal_io: int = Field(default=0, ge=0)
    active_emergency_io: int = Field(default=0, ge=0)
    max_emergency_io: int = Field(default=0, ge=0)
    active_proxy_phase_partitions: int = Field(default=0, ge=0)
    queued_proxy_limited_phase_count: int = Field(default=0, ge=0)
    normal_phase_queue_p50_ms: int = Field(default=0, ge=0)
    normal_phase_queue_p95_ms: int = Field(default=0, ge=0)
    sqlite_write_queue_critical: int = Field(default=0, ge=0)
    sqlite_write_queue_low_priority: int = Field(default=0, ge=0)
    sqlite_write_p95_ms: int = Field(default=0, ge=0)
    actor_count: int = Field(default=0, ge=0)
    event_loop_delay_p99_ms: int = Field(default=0, ge=0)
    open_file_descriptors: int = Field(default=0, ge=0)
    rss_bytes: int = Field(default=0, ge=0)
    market_data_active_leases: int = Field(default=0, ge=0)
    market_data_shared_connections: int = Field(default=0, ge=0)
    market_data_idle_connections: int = Field(default=0, ge=0)
    shared_market_enabled: bool = False
    shared_market_connected: bool = False
    shared_market_generation: int = Field(default=0, ge=0)
    shared_market_btc_snapshot_age_ms: int | None = Field(default=None, ge=0)
    shared_market_eth_snapshot_age_ms: int | None = Field(default=None, ge=0)
    shared_market_rest_fallback_count: int = Field(default=0, ge=0)
    shared_market_reconnect_count: int = Field(default=0, ge=0)
    shared_market_waiting_phase_count: int = Field(default=0, ge=0)
    shared_market_source_state: str = "disabled"
    private_order_stream_active_leases: int = Field(default=0, ge=0)
    private_order_streams: int = Field(default=0, ge=0)
    history_sync_queued: int = Field(default=0, ge=0)
    history_sync_running: int = Field(default=0, ge=0)


class ExecutionCapacityResponse(CamelModel):
    active_executions: int = Field(ge=0)
    max_active_executions: int = Field(ge=1)
    active_normal_phases: int = Field(ge=0)
    max_normal_phases: int = Field(ge=1)
    queued_normal_phases: int = Field(ge=0)
    phase_start_rate_per_second: float = Field(gt=0)
    per_proxy_gap_seconds: float = Field(ge=0)
    revision: int = Field(ge=0)
    active_normal_io: int = Field(ge=0)
    max_normal_io: int = Field(ge=1)
    active_emergency_io: int = Field(ge=0)
    max_emergency_io: int = Field(ge=1)
    active_proxy_phase_partitions: int = Field(ge=0)
    queued_proxy_limited_phases: int = Field(ge=0)
    phase_queue_p50_ms: int = Field(ge=0)
    phase_queue_p95_ms: int = Field(ge=0)
    sqlite_write_queue_critical: int = Field(ge=0)
    sqlite_write_queue_low_priority: int = Field(ge=0)
    sqlite_write_p95_ms: int = Field(ge=0)
    actor_count: int = Field(ge=0)
    event_loop_delay_p99_ms: int = Field(ge=0)
    open_file_descriptors: int = Field(ge=0)
    rss_bytes: int = Field(ge=0)
    market_data_active_leases: int = Field(ge=0)
    market_data_shared_connections: int = Field(ge=0)
    market_data_idle_connections: int = Field(ge=0)
    shared_market_enabled: bool = False
    shared_market_connected: bool = False
    shared_market_generation: int = Field(default=0, ge=0)
    shared_market_btc_snapshot_age_ms: int | None = Field(default=None, ge=0)
    shared_market_eth_snapshot_age_ms: int | None = Field(default=None, ge=0)
    shared_market_rest_fallback_count: int = Field(default=0, ge=0)
    shared_market_reconnect_count: int = Field(default=0, ge=0)
    shared_market_waiting_phase_count: int = Field(default=0, ge=0)
    shared_market_source_state: str = "disabled"
    private_order_stream_active_leases: int = Field(ge=0)
    private_order_streams: int = Field(ge=0)
    history_sync_queued: int = Field(ge=0)
    history_sync_running: int = Field(ge=0)
