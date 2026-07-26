"""Durable Beta Campaign intent and authorization model."""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, DecimalException
from pathlib import Path
from typing import Any

from weex_cli.beta_campaign.allocation import BetaAllocation
from weex_cli.beta_volume import (
    DEFAULT_STRATEGY_DIRECTION,
    DEFAULT_TAKER_DUST_MAX_QUOTE,
    STRATEGY_DIRECTIONS,
    BetaVolumePlan,
)
from weex_cli.core.errors import ValidationError
from weex_cli.core.models import decimal_text, decimal_value
from weex_cli.core.reliability import ReadRetryPolicy
from weex_cli.exchange.rest.gateway import WeexGateway

DEFAULT_CAMPAIGN_DIRECTORY = Path("data/beta-volume-campaigns")
DEFAULT_CHILD_PLAN_DIRECTORY = Path("data/beta-volume-campaign-plans")
DEFAULT_AUTHORIZATION_MINUTES = 360
MAX_CAMPAIGN_RUNS = 20
MAX_STRATEGY_WAIT_SECONDS = 2_592_000.0
MAX_HOLD_SECONDS = MAX_STRATEGY_WAIT_SECONDS
MAX_ROUND_GAP_SECONDS = MAX_STRATEGY_WAIT_SECONDS
DEFAULT_MAX_POSITION_QUOTE = "1200"
DEFAULT_TIMEOUT_SECONDS = 60
DEFAULT_RECOVERY_ATTEMPTS = 3
DEFAULT_MAX_EMPTY_ROUNDS = 3
CAMPAIGN_READ_RETRY_POLICY = ReadRetryPolicy(attempts=8, initial_delay_seconds=1, max_delay_seconds=8)
RETRYABLE_CHILD_REASONS = {"empty_round_limit_exhausted", "round_limit_exhausted"}
CAMPAIGN_CONFIRMATION_PATTERN = re.compile(
    r"EXECUTE WEEX LIVE BETA-CAMPAIGN (?P<campaign_id>WC-[0-9A-F]{10}) "
    r"RUNS_(?:[1-9]|1[0-9]|20) POST_ONLY"
)


@dataclass(frozen=True)
class BetaVolumeCampaign:
    schema_version: int
    campaign_id: str
    created_at_ms: int
    expires_at_ms: int
    profile_fingerprint: str
    target_turnover_quote: Decimal
    # This is the persisted lower bound for a round's total BTC + ETH turnover.
    # `round_turnover_quote` remains the upper bound for compatibility with v1/v2.
    round_turnover_quote_min: Decimal
    round_turnover_quote: Decimal
    max_position_quote: Decimal
    timeout_seconds: int
    recovery_attempts: int
    max_empty_rounds: int
    cooldown_seconds: float
    hold_min_seconds: float
    hold_max_seconds: float
    round_gap_min_seconds: float
    round_gap_max_seconds: float
    max_runs: int
    leverage: str | int
    max_auto_leverage: int
    margin_buffer: Decimal
    margin_mode: str
    allocation: BetaAllocation
    direction: str = DEFAULT_STRATEGY_DIRECTION
    dust_close_max_quote: Decimal = DEFAULT_TAKER_DUST_MAX_QUOTE

    @classmethod
    def create(
        cls,
        gateway: WeexGateway,
        allocation: BetaAllocation,
        *,
        profile_fingerprint: str,
        target_turnover_quote: str | Decimal,
        round_turnover_quote: str | Decimal,
        round_turnover_quote_min: str | Decimal | None = None,
        max_position_quote: str | Decimal = DEFAULT_MAX_POSITION_QUOTE,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        recovery_attempts: int = DEFAULT_RECOVERY_ATTEMPTS,
        max_empty_rounds: int = DEFAULT_MAX_EMPTY_ROUNDS,
        hold_min_seconds: float = 0.0,
        hold_max_seconds: float = 0.0,
        round_gap_min_seconds: float = 1.0,
        round_gap_max_seconds: float = 1.0,
        max_runs: int = MAX_CAMPAIGN_RUNS,
        leverage: str | int = "auto",
        margin_mode: str = "isolated",
        direction: str = DEFAULT_STRATEGY_DIRECTION,
        dust_close_max_quote: str | Decimal = DEFAULT_TAKER_DUST_MAX_QUOTE,
        authorization_minutes: int = DEFAULT_AUTHORIZATION_MINUTES,
        now_ms: int | None = None,
    ) -> BetaVolumeCampaign:
        target = decimal_value(target_turnover_quote, name="target_turnover_quote")
        round_quote = decimal_value(round_turnover_quote, name="round_turnover_quote")
        round_quote_min = decimal_value(
            round_turnover_quote if round_turnover_quote_min is None else round_turnover_quote_min,
            name="round_turnover_quote_min",
        )
        max_position = decimal_value(max_position_quote, name="max_position_quote")
        assert (
            target is not None and round_quote is not None and round_quote_min is not None and max_position is not None
        )
        if not profile_fingerprint or len(profile_fingerprint) < 12:
            raise ValidationError("profile fingerprint is invalid")
        if not 1 <= max_runs <= MAX_CAMPAIGN_RUNS:
            raise ValidationError(f"max_runs must be between 1 and {MAX_CAMPAIGN_RUNS}")
        if not 1 <= authorization_minutes <= 1440:
            raise ValidationError("authorization_minutes must be between 1 and 1440")
        _validate_delay_range("hold", hold_min_seconds, hold_max_seconds, MAX_HOLD_SECONDS)
        _validate_delay_range("round_gap", round_gap_min_seconds, round_gap_max_seconds, MAX_ROUND_GAP_SECONDS)
        round_quote = min(round_quote, target)
        round_quote_min = min(round_quote_min, target)
        if round_quote_min <= 0 or round_quote_min > round_quote:
            raise ValidationError("round turnover minimum must be positive and cannot exceed the maximum")

        # Reuse the production sizing validator so campaign and child plans cannot drift apart.
        preview = BetaVolumePlan.create(
            gateway,
            allocation,
            target_turnover_quote=target,
            round_turnover_quote=round_quote,
            max_position_quote=max_position,
            timeout_seconds=timeout_seconds,
            recovery_attempts=recovery_attempts,
            max_empty_rounds=max_empty_rounds,
            cooldown_seconds=0.0,
            leverage=leverage,
            margin_mode=margin_mode,
            direction=direction,
            dust_close_max_quote=dust_close_max_quote,
            now_ms=now_ms,
        )
        created_at_ms = preview.created_at_ms
        campaign = cls(
            schema_version=5,
            campaign_id="",
            created_at_ms=created_at_ms,
            expires_at_ms=created_at_ms + authorization_minutes * 60_000,
            profile_fingerprint=profile_fingerprint,
            target_turnover_quote=target,
            round_turnover_quote_min=round_quote_min,
            round_turnover_quote=round_quote,
            max_position_quote=max_position,
            timeout_seconds=timeout_seconds,
            recovery_attempts=recovery_attempts,
            max_empty_rounds=max_empty_rounds,
            cooldown_seconds=0.0,
            hold_min_seconds=float(hold_min_seconds),
            hold_max_seconds=float(hold_max_seconds),
            round_gap_min_seconds=float(round_gap_min_seconds),
            round_gap_max_seconds=float(round_gap_max_seconds),
            max_runs=max_runs,
            leverage=preview.leverage,
            max_auto_leverage=preview.max_auto_leverage,
            margin_buffer=preview.margin_buffer,
            margin_mode=preview.margin_mode,
            direction=preview.direction,
            dust_close_max_quote=preview.dust_close_max_quote,
            allocation=allocation,
        )
        return campaign._with_computed_id()

    @property
    def authorized_max_turnover_quote(self) -> Decimal:
        return self.target_turnover_quote + self.round_turnover_quote

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "campaign_id": self.campaign_id,
            "created_at_ms": self.created_at_ms,
            "expires_at_ms": self.expires_at_ms,
            "mode": "live",
            "strategy": self.direction,
            "profile_fingerprint": self.profile_fingerprint,
            "target_turnover_quote": decimal_text(self.target_turnover_quote),
            "round_turnover_quote_min": decimal_text(self.round_turnover_quote_min),
            "round_turnover_quote": decimal_text(self.round_turnover_quote),
            "authorized_max_turnover_quote": decimal_text(self.authorized_max_turnover_quote),
            "max_position_quote": decimal_text(self.max_position_quote),
            "timeout_seconds": self.timeout_seconds,
            "recovery_attempts": self.recovery_attempts,
            "max_empty_rounds": self.max_empty_rounds,
            "cooldown_seconds": self.cooldown_seconds,
            "hold_min_seconds": self.hold_min_seconds,
            "hold_max_seconds": self.hold_max_seconds,
            "round_gap_min_seconds": self.round_gap_min_seconds,
            "round_gap_max_seconds": self.round_gap_max_seconds,
            "max_runs": self.max_runs,
            "leverage": self.leverage,
            "max_auto_leverage": self.max_auto_leverage,
            "margin_buffer": decimal_text(self.margin_buffer),
            "margin_mode": self.margin_mode,
            "direction": self.direction,
            "dust_close_max_quote": decimal_text(self.dust_close_max_quote),
            "time_in_force": "POST_ONLY",
            "allocation": self.allocation.as_dict(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> BetaVolumeCampaign:
        allocation_row = payload.get("allocation")
        if not isinstance(allocation_row, Mapping):
            raise ValidationError("stored campaign allocation is invalid")
        try:
            allocation = BetaAllocation(
                beta=Decimal(str(allocation_row["beta"])),
                btc_long_weight=Decimal(str(allocation_row["btc_long_weight"])),
                eth_short_weight=Decimal(str(allocation_row["eth_short_weight"])),
                version=str(allocation_row["version"]),
                as_of_ms=int(allocation_row["as_of_ms"]),
                confidence=Decimal(str(allocation_row["confidence"])),
                confidence_threshold=Decimal(str(allocation_row["confidence_threshold"])),
                source=str(allocation_row["source"]),
                confidence_override=bool(allocation_row.get("confidence_override", False)),
            )
            schema_version = int(payload["schema_version"])
            cooldown_seconds = float(payload.get("cooldown_seconds", 0))
            campaign = cls(
                schema_version=schema_version,
                campaign_id=str(payload["campaign_id"]).lower(),
                created_at_ms=int(payload["created_at_ms"]),
                expires_at_ms=int(payload["expires_at_ms"]),
                profile_fingerprint=str(payload["profile_fingerprint"]),
                target_turnover_quote=Decimal(str(payload["target_turnover_quote"])),
                round_turnover_quote_min=Decimal(
                    str(payload.get("round_turnover_quote_min", payload["round_turnover_quote"]))
                ),
                round_turnover_quote=Decimal(str(payload["round_turnover_quote"])),
                max_position_quote=Decimal(str(payload["max_position_quote"])),
                timeout_seconds=int(payload["timeout_seconds"]),
                recovery_attempts=int(payload["recovery_attempts"]),
                max_empty_rounds=int(payload["max_empty_rounds"]),
                cooldown_seconds=cooldown_seconds,
                hold_min_seconds=float(payload.get("hold_min_seconds", 0)),
                hold_max_seconds=float(payload.get("hold_max_seconds", 0)),
                round_gap_min_seconds=float(payload.get("round_gap_min_seconds", cooldown_seconds)),
                round_gap_max_seconds=float(payload.get("round_gap_max_seconds", cooldown_seconds)),
                max_runs=int(payload["max_runs"]),
                leverage=str(payload["leverage"]) if payload["leverage"] == "auto" else int(payload["leverage"]),
                max_auto_leverage=int(payload["max_auto_leverage"]),
                margin_buffer=Decimal(str(payload["margin_buffer"])),
                margin_mode=str(payload["margin_mode"]),
                direction=str(payload.get("direction", payload.get("strategy", DEFAULT_STRATEGY_DIRECTION))),
                dust_close_max_quote=Decimal(str(payload.get("dust_close_max_quote", DEFAULT_TAKER_DUST_MAX_QUOTE))),
                allocation=allocation,
            )
        except (DecimalException, KeyError, TypeError, ValueError) as exc:
            raise ValidationError("stored campaign payload is invalid") from exc
        if (
            campaign.schema_version not in {1, 2, 3, 4, 5}
            or not 1 <= campaign.max_runs <= MAX_CAMPAIGN_RUNS
            or campaign.expires_at_ms <= campaign.created_at_ms
            or campaign.expires_at_ms - campaign.created_at_ms > 86_400_000
            or campaign.target_turnover_quote <= 0
            or campaign.round_turnover_quote <= 0
            or campaign.round_turnover_quote_min <= 0
            or campaign.round_turnover_quote_min > campaign.round_turnover_quote
            or campaign.round_turnover_quote > campaign.target_turnover_quote
            or campaign.max_position_quote <= 0
            or campaign.direction not in STRATEGY_DIRECTIONS
            or not _valid_delay_range(
                campaign.hold_min_seconds,
                campaign.hold_max_seconds,
                MAX_HOLD_SECONDS,
            )
            or not _valid_delay_range(
                campaign.round_gap_min_seconds,
                campaign.round_gap_max_seconds,
                MAX_ROUND_GAP_SECONDS,
            )
            or campaign.campaign_id != campaign._computed_id()
        ):
            raise ValidationError("stored campaign identity is invalid")
        return campaign

    def _with_computed_id(self) -> BetaVolumeCampaign:
        return BetaVolumeCampaign(**{**self.__dict__, "campaign_id": self._computed_id()})

    def _computed_id(self) -> str:
        fields = [
            str(self.schema_version),
            str(self.created_at_ms),
            str(self.expires_at_ms),
            self.profile_fingerprint,
            decimal_text(self.target_turnover_quote) or "0",
            decimal_text(self.round_turnover_quote) or "0",
            decimal_text(self.max_position_quote) or "0",
            str(self.timeout_seconds),
            str(self.recovery_attempts),
            str(self.max_empty_rounds),
            str(self.cooldown_seconds),
        ]
        if self.schema_version >= 2:
            fields.extend(
                (
                    str(self.hold_min_seconds),
                    str(self.hold_max_seconds),
                    str(self.round_gap_min_seconds),
                    str(self.round_gap_max_seconds),
                )
            )
        if self.schema_version >= 3:
            fields.append(decimal_text(self.round_turnover_quote_min) or "0")
        if self.schema_version >= 4:
            fields.append(self.direction)
        if self.schema_version >= 5:
            fields.append(decimal_text(self.dust_close_max_quote) or "0")
        fields.extend(
            (
                str(self.max_runs),
                str(self.leverage),
                str(self.max_auto_leverage),
                decimal_text(self.margin_buffer) or "0",
                self.margin_mode,
                self.allocation.version,
                decimal_text(self.allocation.beta) or "0",
                decimal_text(self.allocation.btc_long_weight) or "0",
                decimal_text(self.allocation.eth_short_weight) or "0",
                str(self.allocation.as_of_ms),
            )
        )
        identity = "|".join(fields)
        return f"wc-{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:10]}"


def _validate_delay_range(name: str, minimum: float, maximum: float, ceiling: float) -> None:
    if not _valid_delay_range(minimum, maximum, ceiling):
        raise ValidationError(f"{name} range must be finite, non-negative, ordered, and at most {ceiling:g} seconds")


def _valid_delay_range(minimum: float, maximum: float, ceiling: float) -> bool:
    return math.isfinite(minimum) and math.isfinite(maximum) and 0 <= minimum <= maximum <= ceiling
