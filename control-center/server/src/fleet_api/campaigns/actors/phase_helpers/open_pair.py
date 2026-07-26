"""Build and submit the two synchronized opening legs for one frozen cycle."""

from __future__ import annotations

from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from typing import Any

from weex_cli.control_api.exchange import decimal_text
from weex_cli.control_api.volume import CycleLegSpec, signed_open_quantity


def open_pair(
    service: Any,
    plan: Any,
    round_number: int,
    planned_turnover_quote: Decimal,
    remaining_target_quote: Decimal,
    btc_plan: Any,
    eth_plan: Any,
    lanes: Mapping[str, Any],
) -> Mapping[str, tuple[dict[str, Any], tuple[str, str] | None]]:
    service._emit(
        "cycle_started",
        round=round_number,
        attempt=plan.plan_id.rsplit("-a", 1)[-1],
        desired_quote=decimal_text(planned_turnover_quote),
        planned_turnover_quote=decimal_text(planned_turnover_quote),
        target_quote=decimal_text(plan.target_turnover_quote),
        remaining_quote=decimal_text(remaining_target_quote),
        opening_notional_quote=decimal_text(
            btc_plan.quantity * btc_plan.reference_price + eth_plan.quantity * eth_plan.reference_price
        ),
        btc_quantity=decimal_text(btc_plan.quantity),
        eth_quantity=decimal_text(eth_plan.quantity),
        leverage=plan.leverage,
    )
    specs = {
        "BTC": CycleLegSpec(
            btc_plan,
            "open",
            btc_plan.opening_side,
            signed_open_quantity(btc_plan),
            f"{plan.plan_id}-r{round_number:03d}-bo",
        ),
        "ETH": CycleLegSpec(
            eth_plan,
            "open",
            eth_plan.opening_side,
            signed_open_quantity(eth_plan),
            f"{plan.plan_id}-r{round_number:03d}-eo",
        ),
    }
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="fleet-open") as pool:
        return service._run_pair(pool, plan, round_number, 1, specs, lanes)
