from __future__ import annotations

import hashlib
import math
import time
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_UP, Decimal
from typing import Any

from weex_cli.beta_campaign.allocation import BetaAllocation
from weex_cli.core.errors import ValidationError
from weex_cli.core.models import decimal_text, decimal_value
from weex_cli.exchange.rest.gateway import WeexGateway

from .contracts import (
    DEFAULT_STRATEGY_DIRECTION,
    DEFAULT_TAKER_DUST_MAX_QUOTE,
    MARGIN_BUFFER,
    MAX_AUTO_LEVERAGE,
    PLAN_MAX_AGE_SECONDS,
    PairLegPlan,
)
from .safety import _direction_sides, _normalize_direction, _normalize_leverage, _normalize_margin_mode
from .sizing import _choose_pair_quantities, _mid_price


@dataclass(frozen=True)
class BetaVolumePlan:
    schema_version: int
    plan_id: str
    created_at_ms: int
    target_turnover_quote: Decimal
    round_turnover_quote: Decimal
    opening_budget_quote: Decimal
    max_position_quote: Decimal
    timeout_seconds: int
    recovery_attempts: int
    max_empty_rounds: int
    cooldown_seconds: float
    leverage: str | int
    max_auto_leverage: int
    margin_buffer: Decimal
    margin_mode: str
    allocation: BetaAllocation
    btc: PairLegPlan
    eth: PairLegPlan
    estimated_turnover_quote: Decimal
    direction: str = DEFAULT_STRATEGY_DIRECTION
    dust_close_max_quote: Decimal = DEFAULT_TAKER_DUST_MAX_QUOTE

    @classmethod
    def create(
        cls,
        gateway: WeexGateway,
        allocation: BetaAllocation,
        *,
        target_turnover_quote: str | Decimal,
        round_turnover_quote: str | Decimal = "500",
        max_position_quote: str | Decimal,
        timeout_seconds: int,
        recovery_attempts: int = 3,
        max_empty_rounds: int = 3,
        cooldown_seconds: float = 1.0,
        leverage: str | int = "auto",
        max_auto_leverage: int = MAX_AUTO_LEVERAGE,
        margin_buffer: str | Decimal = MARGIN_BUFFER,
        margin_mode: str = "isolated",
        direction: str = DEFAULT_STRATEGY_DIRECTION,
        dust_close_max_quote: str | Decimal = DEFAULT_TAKER_DUST_MAX_QUOTE,
        now_ms: int | None = None,
    ) -> BetaVolumePlan:
        target = decimal_value(target_turnover_quote, name="target_turnover_quote")
        per_round = decimal_value(round_turnover_quote, name="round_turnover_quote")
        max_position = decimal_value(max_position_quote, name="max_position_quote")
        assert target is not None and per_round is not None and max_position is not None
        normalized_leverage = _normalize_leverage(leverage)
        normalized_margin_mode = _normalize_margin_mode(margin_mode)
        normalized_direction = _normalize_direction(direction)
        normalized_margin_buffer = decimal_value(margin_buffer, name="margin_buffer")
        assert normalized_margin_buffer is not None
        if timeout_seconds < 1:
            raise ValidationError("timeout_seconds must be positive")
        if not 1 <= max_auto_leverage <= 125:
            raise ValidationError("max_auto_leverage must be between 1 and 125")
        if normalized_margin_buffer < 1:
            raise ValidationError("margin_buffer must be at least 1")
        if not 1 <= recovery_attempts <= 10:
            raise ValidationError("recovery_attempts must be between 1 and 10")
        if not 0 <= max_empty_rounds <= 20:
            raise ValidationError("max_empty_rounds must be between 0 and 20")
        if not math.isfinite(cooldown_seconds) or not 0 <= cooldown_seconds <= 300:
            raise ValidationError("cooldown_seconds must be finite and between 0 and 300")
        per_round = min(per_round, target)
        opening_budget = per_round / 2
        btc_quote = opening_budget * allocation.btc_long_weight
        eth_quote = opening_budget * allocation.eth_short_weight
        btc_price = _mid_price(gateway, "BTC")
        eth_price = _mid_price(gateway, "ETH")
        btc_step = gateway.amount_step("BTC")
        eth_step = gateway.amount_step("ETH")

        minimum_target = max(
            Decimal(2) * btc_step * btc_price / allocation.btc_long_weight,
            Decimal(2) * eth_step * eth_price / allocation.eth_short_weight,
        )
        if btc_quote < btc_step * btc_price or eth_quote < eth_step * eth_price:
            suggested = minimum_target.quantize(Decimal("0.01"), rounding=ROUND_UP)
            raise ValidationError(
                "target turnover is below the current BTC/ETH minimum-size allocation; "
                f"current minimum is approximately {suggested} USDT"
            )

        btc_quantity, eth_quantity, estimated_turnover = _choose_pair_quantities(
            gateway,
            per_round,
            btc_quote,
            eth_quote,
            btc_price,
            eth_price,
            btc_step,
            eth_step,
        )
        if btc_quantity * btc_price >= max_position or eth_quantity * eth_price >= max_position:
            raise ValidationError("a planned leg reaches or exceeds max_position_quote")

        created_at_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        identity = "|".join(
            (
                allocation.version,
                decimal_text(target) or "0",
                decimal_text(per_round) or "0",
                decimal_text(btc_quantity) or "0",
                decimal_text(eth_quantity) or "0",
                decimal_text(max_position) or "0",
                str(timeout_seconds),
                str(recovery_attempts),
                str(max_empty_rounds),
                str(cooldown_seconds),
                str(normalized_leverage),
                str(max_auto_leverage),
                decimal_text(normalized_margin_buffer) or "0",
                normalized_margin_mode,
                normalized_direction,
                str(created_at_ms),
            )
        )
        digest = hashlib.sha256(identity.encode("ascii")).hexdigest()[:10]
        plan_id = f"wv-{digest}"
        btc_position, btc_open, btc_close = _direction_sides(normalized_direction, "BTC")
        eth_position, eth_open, eth_close = _direction_sides(normalized_direction, "ETH")
        btc = PairLegPlan(
            symbol="BTC",
            position_side=btc_position,
            opening_side=btc_open,
            closing_side=btc_close,
            allocated_quote=btc_quote,
            reference_price=btc_price,
            quantity=btc_quantity,
            amount_step=btc_step,
            open_client_prefix=f"{plan_id}-bo",
            close_client_prefix=f"{plan_id}-bc",
        )
        eth = PairLegPlan(
            symbol="ETH",
            position_side=eth_position,
            opening_side=eth_open,
            closing_side=eth_close,
            allocated_quote=eth_quote,
            reference_price=eth_price,
            quantity=eth_quantity,
            amount_step=eth_step,
            open_client_prefix=f"{plan_id}-eo",
            close_client_prefix=f"{plan_id}-ec",
        )
        dust_limit = decimal_value(dust_close_max_quote, name="dust_close_max_quote")
        assert dust_limit is not None
        return cls(
            schema_version=5,
            plan_id=plan_id,
            created_at_ms=created_at_ms,
            target_turnover_quote=target,
            round_turnover_quote=per_round,
            opening_budget_quote=opening_budget,
            max_position_quote=max_position,
            timeout_seconds=timeout_seconds,
            recovery_attempts=recovery_attempts,
            max_empty_rounds=max_empty_rounds,
            cooldown_seconds=cooldown_seconds,
            leverage=normalized_leverage,
            max_auto_leverage=max_auto_leverage,
            margin_buffer=normalized_margin_buffer,
            margin_mode=normalized_margin_mode,
            direction=normalized_direction,
            dust_close_max_quote=dust_limit,
            allocation=allocation,
            btc=btc,
            eth=eth,
            estimated_turnover_quote=estimated_turnover,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "schema_version": self.schema_version,
            "created_at_ms": self.created_at_ms,
            "expires_at_ms": self.created_at_ms + PLAN_MAX_AGE_SECONDS * 1000,
            "mode": "live",
            "strategy": self.direction,
            "target_turnover_quote": decimal_text(self.target_turnover_quote),
            "round_turnover_quote": decimal_text(self.round_turnover_quote),
            "opening_budget_quote": decimal_text(self.opening_budget_quote),
            "max_position_quote": decimal_text(self.max_position_quote),
            "timeout_seconds": self.timeout_seconds,
            "recovery_attempts": self.recovery_attempts,
            "max_empty_rounds": self.max_empty_rounds,
            "cooldown_seconds": self.cooldown_seconds,
            "leverage": self.leverage,
            "max_auto_leverage": self.max_auto_leverage,
            "margin_buffer": decimal_text(self.margin_buffer),
            "margin_mode": self.margin_mode,
            "direction": self.direction,
            "dust_close_max_quote": decimal_text(self.dust_close_max_quote),
            "minimum_available_quote": decimal_text(self.required_available_quote),
            "allocation": self.allocation.as_dict(),
            "legs": [self.btc.as_dict(), self.eth.as_dict()],
            "estimated_turnover_quote": decimal_text(self.estimated_turnover_quote),
            "estimated_rounds": self.estimated_rounds,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> BetaVolumePlan:
        allocation_row = payload["allocation"]
        legs = payload["legs"]
        if not isinstance(allocation_row, Mapping) or not isinstance(legs, list) or len(legs) != 2:
            raise ValidationError("stored Beta plan is invalid")
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
        return cls(
            schema_version=int(payload.get("schema_version", 1)),
            plan_id=str(payload["plan_id"]),
            created_at_ms=int(payload["created_at_ms"]),
            target_turnover_quote=Decimal(str(payload["target_turnover_quote"])),
            round_turnover_quote=Decimal(str(payload.get("round_turnover_quote", payload["target_turnover_quote"]))),
            opening_budget_quote=Decimal(str(payload["opening_budget_quote"])),
            max_position_quote=Decimal(str(payload["max_position_quote"])),
            timeout_seconds=int(payload["timeout_seconds"]),
            recovery_attempts=int(payload.get("recovery_attempts", 3)),
            max_empty_rounds=int(payload.get("max_empty_rounds", 3)),
            cooldown_seconds=float(payload.get("cooldown_seconds", 1.0)),
            leverage=_normalize_leverage(payload.get("leverage", 1)),
            max_auto_leverage=int(payload.get("max_auto_leverage", MAX_AUTO_LEVERAGE)),
            margin_buffer=Decimal(str(payload.get("margin_buffer", MARGIN_BUFFER))),
            margin_mode=_normalize_margin_mode(payload.get("margin_mode", "isolated")),
            direction=_normalize_direction(
                payload.get("direction", payload.get("strategy", DEFAULT_STRATEGY_DIRECTION))
            ),
            dust_close_max_quote=Decimal(str(payload.get("dust_close_max_quote", DEFAULT_TAKER_DUST_MAX_QUOTE))),
            allocation=allocation,
            btc=_leg_from_dict(legs[0]),
            eth=_leg_from_dict(legs[1]),
            estimated_turnover_quote=Decimal(str(payload["estimated_turnover_quote"])),
        )

    @property
    def required_available_quote(self) -> Decimal:
        opening_notional = self.estimated_turnover_quote / 2
        leverage = self.max_auto_leverage if self.leverage == "auto" else int(self.leverage)
        return opening_notional / Decimal(leverage) * self.margin_buffer

    @property
    def estimated_rounds(self) -> int:
        return int((self.target_turnover_quote / self.round_turnover_quote).to_integral_value(rounding=ROUND_UP))


def _leg_from_dict(payload: Any) -> PairLegPlan:
    if not isinstance(payload, Mapping):
        raise ValidationError("stored Beta leg is invalid")
    return PairLegPlan(
        symbol=str(payload["symbol"]),
        position_side=str(payload["position_side"]),
        opening_side=str(payload["opening_side"]),
        closing_side=str(payload["closing_side"]),
        allocated_quote=Decimal(str(payload["allocated_quote"])),
        reference_price=Decimal(str(payload["reference_price"])),
        quantity=Decimal(str(payload["quantity"])),
        amount_step=Decimal(str(payload["amount_step"])),
        open_client_prefix=str(payload["open_client_order_id"]).removesuffix("-001"),
        close_client_prefix=str(payload["close_client_order_id"]).removesuffix("-001"),
    )
