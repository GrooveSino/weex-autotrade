"""Stable plans and confirmation payloads for live Maker volume sessions."""

from __future__ import annotations

import hashlib
import time
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_UP, Decimal
from pathlib import Path
from typing import Any

from weex_cli.core.errors import ValidationError
from weex_cli.core.models import decimal_text, decimal_value
from weex_cli.exchange.rest.gateway import WeexGateway

from .support import mid_price, quantity_for_turnover

PLAN_MAX_AGE_SECONDS = 900
MARGIN_BUFFER = Decimal("1.20")
POSITION_BUFFER = Decimal("1.10")
DEFAULT_PLAN_DIRECTORY = Path("data/live-maker-volume-plans")


@dataclass(frozen=True)
class LiveMakerVolumePlan:
    plan_id: str
    created_at_ms: int
    symbol: str
    target_quote: Decimal
    round_quote: Decimal
    reference_price: Decimal
    amount_step: Decimal
    max_position_quote: Decimal
    timeout_seconds: int
    recovery_attempts: int
    max_empty_rounds: int
    cooldown_seconds: float
    leverage: int

    @classmethod
    def create(
        cls,
        gateway: WeexGateway,
        *,
        symbol: str,
        target_quote: str | Decimal,
        round_quote: str | Decimal,
        timeout_seconds: int,
        recovery_attempts: int = 3,
        max_empty_rounds: int = 3,
        cooldown_seconds: float = 1.0,
        leverage: int = 1,
        now_ms: int | None = None,
    ) -> LiveMakerVolumePlan:
        target = decimal_value(target_quote, name="target_quote")
        per_round = decimal_value(round_quote, name="round_quote")
        assert target is not None and per_round is not None
        normalized_symbol = symbol.strip().upper()
        if not normalized_symbol:
            raise ValidationError("symbol is required")
        if timeout_seconds < 1:
            raise ValidationError("timeout_seconds must be positive")
        if not 1 <= recovery_attempts <= 10:
            raise ValidationError("recovery_attempts must be between 1 and 10")
        if not 0 <= max_empty_rounds <= 20:
            raise ValidationError("max_empty_rounds must be between 0 and 20")
        if not 0 <= cooldown_seconds <= 300:
            raise ValidationError("cooldown_seconds must be between 0 and 300")
        if not 1 <= leverage <= 125:
            raise ValidationError("leverage must be between 1 and 125")
        if per_round > target:
            per_round = target

        price = mid_price(gateway, normalized_symbol)
        step = gateway.amount_step(normalized_symbol)
        quantity = quantity_for_turnover(gateway, normalized_symbol, per_round, price)
        max_position = max(per_round / 2, quantity * price) * POSITION_BUFFER
        created = now_ms if now_ms is not None else int(time.time() * 1000)
        identity = "|".join(
            (
                normalized_symbol,
                decimal_text(target) or "0",
                decimal_text(per_round) or "0",
                str(timeout_seconds),
                str(recovery_attempts),
                str(max_empty_rounds),
                str(cooldown_seconds),
                str(leverage),
                str(created),
            )
        )
        plan_id = f"lmv-{hashlib.sha256(identity.encode('ascii')).hexdigest()[:10]}"
        return cls(
            plan_id=plan_id,
            created_at_ms=created,
            symbol=normalized_symbol,
            target_quote=target,
            round_quote=per_round,
            reference_price=price,
            amount_step=step,
            max_position_quote=max_position,
            timeout_seconds=timeout_seconds,
            recovery_attempts=recovery_attempts,
            max_empty_rounds=max_empty_rounds,
            cooldown_seconds=cooldown_seconds,
            leverage=leverage,
        )

    @property
    def required_available_quote(self) -> Decimal:
        return self.max_position_quote / Decimal(self.leverage) * MARGIN_BUFFER

    @property
    def estimated_rounds(self) -> int:
        return int((self.target_quote / self.round_quote).to_integral_value(rounding=ROUND_UP))

    def as_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "created_at_ms": self.created_at_ms,
            "expires_at_ms": self.created_at_ms + PLAN_MAX_AGE_SECONDS * 1000,
            "mode": "live",
            "strategy": "alternating_flat_to_flat",
            "symbol": self.symbol,
            "target_quote": decimal_text(self.target_quote),
            "round_quote": decimal_text(self.round_quote),
            "reference_price": decimal_text(self.reference_price),
            "amount_step": decimal_text(self.amount_step),
            "max_position_quote": decimal_text(self.max_position_quote),
            "required_available_quote": decimal_text(self.required_available_quote),
            "timeout_seconds": self.timeout_seconds,
            "recovery_attempts": self.recovery_attempts,
            "max_empty_rounds": self.max_empty_rounds,
            "cooldown_seconds": self.cooldown_seconds,
            "leverage": self.leverage,
            "estimated_rounds": self.estimated_rounds,
            "time_in_force": "POST_ONLY",
            "volume_source": "userTrades.quoteQty",
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> LiveMakerVolumePlan:
        return cls(
            plan_id=str(payload["plan_id"]),
            created_at_ms=int(payload["created_at_ms"]),
            symbol=str(payload["symbol"]),
            target_quote=Decimal(str(payload["target_quote"])),
            round_quote=Decimal(str(payload["round_quote"])),
            reference_price=Decimal(str(payload["reference_price"])),
            amount_step=Decimal(str(payload["amount_step"])),
            max_position_quote=Decimal(str(payload["max_position_quote"])),
            timeout_seconds=int(payload["timeout_seconds"]),
            recovery_attempts=int(payload["recovery_attempts"]),
            max_empty_rounds=int(payload["max_empty_rounds"]),
            cooldown_seconds=float(payload["cooldown_seconds"]),
            leverage=int(payload["leverage"]),
        )


def live_maker_volume_confirmation(plan: LiveMakerVolumePlan) -> str:
    return " ".join(
        (
            "EXECUTE WEEX LIVE MAKER VOLUME",
            plan.symbol,
            f"TARGET_{decimal_text(plan.target_quote)}",
            f"ROUND_{decimal_text(plan.round_quote)}",
            f"LEVERAGE_{plan.leverage}",
            f"TIMEOUT_{plan.timeout_seconds}",
            f"RECOVERY_{plan.recovery_attempts}",
            f"EMPTY_{plan.max_empty_rounds}",
            "POST_ONLY",
            plan.plan_id.upper(),
        )
    )


def plan_payload(plan: LiveMakerVolumePlan, path: Path) -> dict[str, Any]:
    return {
        "status": "dry_run",
        "kind": "live_maker_volume_plan",
        "action": "live_maker_volume",
        "mode": "live",
        "plan": plan.as_dict(),
        "plan_file": str(path),
        "confirm": live_maker_volume_confirmation(plan),
    }
