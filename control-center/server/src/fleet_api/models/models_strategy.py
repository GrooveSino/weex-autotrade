from __future__ import annotations

from decimal import Decimal
from typing import Any, Self

from pydantic import Field, SecretStr
from pydantic.functional_validators import model_validator

from fleet_api.models.models_shared import CamelModel, ProxyType, StrategyStage, StrategyTargetMode


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
    target_volume_quote_min: Decimal = Field(gt=0, le=1_000_000_000_000, multiple_of=Decimal("0.01"))
    target_volume_quote_max: Decimal = Field(gt=0, le=1_000_000_000_000, multiple_of=Decimal("0.01"))
    round_turnover_quote_min: Decimal = Field(gt=0, le=1_000_000_000, multiple_of=Decimal("0.01"))
    round_turnover_quote_max: Decimal = Field(gt=0, le=1_000_000_000, multiple_of=Decimal("0.01"))
    position_hold_min_seconds: int = Field(default=5, ge=0, le=2_592_000)
    position_hold_max_seconds: int = Field(default=15, ge=0, le=2_592_000)
    round_interval_min_seconds: int = Field(default=10, ge=0, le=2_592_000)
    round_interval_max_seconds: int = Field(default=30, ge=0, le=2_592_000)

    @model_validator(mode="before")
    @classmethod
    def normalize_target_range(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        payload = dict(value)
        legacy = payload.pop("targetVolumeQuote", payload.get("target_volume_quote"))
        minimum = payload.pop("targetVolumeQuoteMin", payload.get("target_volume_quote_min", legacy))
        maximum = payload.pop("targetVolumeQuoteMax", payload.get("target_volume_quote_max", legacy))
        if minimum is not None:
            payload["target_volume_quote_min"] = minimum
        if maximum is not None:
            payload["target_volume_quote_max"] = maximum
            payload["target_volume_quote"] = maximum
        return payload

    @model_validator(mode="after")
    def validate_ranges(self) -> Self:
        if self.target_volume_quote_min > self.target_volume_quote_max:
            raise ValueError("target volume minimum cannot exceed maximum")
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
