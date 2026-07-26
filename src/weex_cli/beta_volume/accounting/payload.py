from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from weex_cli.core.models import decimal_text
from weex_cli.execution.venues import LiveAdaptiveMakerVenue

from ..plan import BetaVolumePlan
from ..safety import _safe_position
from .fills import accounting_summary


def _result_payload(
    plan: BetaVolumePlan,
    status: str,
    reason: str,
    legs: list[dict[str, Any]],
    cycles: list[dict[str, Any]],
    total_quote: Decimal,
    venues: Mapping[str, LiveAdaptiveMakerVenue],
    preflight: Mapping[str, Any],
    timeline: list[dict[str, Any]],
    elapsed_ms: int,
) -> dict[str, Any]:
    accounting = accounting_summary(legs)
    achievement = total_quote / plan.target_turnover_quote * Decimal(100)
    excess = max(Decimal(0), total_quote - plan.target_turnover_quote)
    shortfall = max(Decimal(0), plan.target_turnover_quote - total_quote)
    return {
        "schema_version": plan.schema_version,
        "kind": "beta_volume_execution",
        "mode": "live",
        "strategy": plan.direction,
        "status": status,
        "reason": reason,
        "plan_id": plan.plan_id,
        "maker_only": accounting["maker_only"],
        "liquidity_policy_satisfied": accounting["liquidity_policy_satisfied"],
        "executed_quote_volume": decimal_text(total_quote),
        "target_turnover_quote": decimal_text(plan.target_turnover_quote),
        "round_turnover_quote": decimal_text(plan.round_turnover_quote),
        "remaining_quote": "0" if status == "completed" else decimal_text(shortfall),
        "target_shortfall_quote": decimal_text(shortfall),
        "excess_quote": decimal_text(excess),
        "target_achievement_percent": decimal_text(achievement),
        "elapsed_ms": elapsed_ms,
        "accounting": accounting,
        "legs": legs,
        "cycles": cycles,
        "final_positions": {
            f"BTC_{plan.btc.position_side}".upper(): _safe_position(venues["BTC"]),
            f"ETH_{plan.eth.position_side}".upper(): _safe_position(venues["ETH"]),
        },
        "preflight": dict(preflight),
        "timeline": list(timeline),
        "reconciliation_required": status not in {"completed", "executing"},
        "retry_allowed": False,
        "recovery": "Stop. Inspect positions and orders, then create a separately confirmed pure-Maker flatten plan.",
    }
