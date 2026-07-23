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
