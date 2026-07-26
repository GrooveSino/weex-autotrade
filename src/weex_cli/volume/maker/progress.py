"""Progress and result projections for demo maker-volume batches."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from weex_cli.core.models import decimal_text

from .contracts import MakerVolumePlan


@dataclass
class BatchProgress:
    plan: MakerVolumePlan
    prefix: str
    started: float
    attempts: list[dict[str, Any]] = field(default_factory=list)
    fills: list[dict[str, Any]] = field(default_factory=list)
    total_quote: Decimal = Decimal("0")
    opening_quote: Decimal = Decimal("0")
    closing_quote: Decimal = Decimal("0")
    open_quantity: Decimal | None = None
    last_submit_at: float | None = None

    def record_fill(self, action: str, outcome: dict[str, Any]) -> None:
        quote = Decimal(str(outcome["quote_volume"]))
        self.total_quote += quote
        if action == "open":
            self.opening_quote += quote
        else:
            self.closing_quote += quote
            self.open_quantity = None
        excluded = {"batch_status", "reason", "position"}
        self.fills.append({key: value for key, value in outcome.items() if key not in excluded})


def finish(
    progress: BatchProgress,
    status: str,
    reason: str,
    position: dict[str, Any],
    *,
    now: float,
) -> dict[str, Any]:
    plan = progress.plan
    return {
        "status": status,
        "reason": reason,
        "plan": plan.as_dict(),
        "client_prefix": progress.prefix,
        "attempt_count": len(progress.attempts),
        "fill_count": len(progress.fills),
        "total_quote_volume": decimal_text(progress.total_quote),
        "opening_quote_volume": decimal_text(progress.opening_quote),
        "closing_quote_volume": decimal_text(progress.closing_quote),
        "target_met": progress.total_quote >= plan.target_quote,
        "final_position": position,
        "elapsed_seconds": round(now - progress.started, 3),
        "attempts": progress.attempts,
        "fills": progress.fills,
    }
