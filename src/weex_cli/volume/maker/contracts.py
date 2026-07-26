"""Public plan and confirmation contract for demo maker-volume batches."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from weex_cli.core.errors import ValidationError
from weex_cli.core.models import decimal_text, decimal_value
from weex_cli.core.symbols import base_asset

VOLUME_BUFFER = Decimal("1.01")


@dataclass(frozen=True)
class MakerVolumePlan:
    symbol: str
    target_quote: Decimal
    fills: int
    max_position_quote: Decimal
    timeout_seconds: int
    poll_interval_seconds: float = 1.0

    @classmethod
    def create(
        cls,
        *,
        symbol: str,
        target_quote: str | Decimal,
        fills: int,
        max_position_quote: str | Decimal,
        timeout_seconds: int,
        poll_interval_seconds: float = 1.0,
    ) -> MakerVolumePlan:
        target = decimal_value(target_quote, name="target_quote")
        max_position = decimal_value(max_position_quote, name="max_position_quote")
        assert target is not None and max_position is not None
        if fills < 2 or fills % 2:
            raise ValidationError("fills must be an even integer of at least 2 so the batch ends flat")
        if timeout_seconds < 1:
            raise ValidationError("timeout_seconds must be at least 1")
        if not 0.2 <= poll_interval_seconds <= 10:
            raise ValidationError("poll_interval_seconds must be between 0.2 and 10")
        if target / fills * VOLUME_BUFFER >= max_position:
            raise ValidationError("target is infeasible for the fill count and max position with the safety buffer")
        return cls(
            symbol=base_asset(symbol),
            target_quote=target,
            fills=fills,
            max_position_quote=max_position,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "mode": "demo",
            "symbol": self.symbol,
            "target_quote_volume": decimal_text(self.target_quote),
            "fills": self.fills,
            "cycles": self.fills // 2,
            "max_position_quote": decimal_text(self.max_position_quote),
            "timeout_seconds_per_order": self.timeout_seconds,
            "poll_interval_seconds": self.poll_interval_seconds,
            "volume_buffer_percent": "1",
        }


def maker_volume_confirmation(plan: MakerVolumePlan) -> str:
    return " ".join(
        [
            "EXECUTE",
            "WEEX",
            "DEMO",
            "MAKER",
            "VOLUME",
            plan.symbol,
            f"TARGET_{decimal_text(plan.target_quote)}",
            f"FILLS_{plan.fills}",
            f"MAX_POSITION_{decimal_text(plan.max_position_quote)}",
            f"TIMEOUT_{plan.timeout_seconds}",
        ]
    )
