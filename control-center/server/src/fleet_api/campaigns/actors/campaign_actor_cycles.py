"""Pure calculations and bounded lane helpers for Fleet Campaign actors."""

from __future__ import annotations

import random
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from typing import Any

from weex_cli.beta_volume import (
    LiveBetaVolumeService,
    PairLegPlan,
    _accounting_summary,
    _Lane,
    _owned_position_quantity,
    _signed_open_quantity,
)
from weex_cli.errors import SafetyError
from weex_cli.models import decimal_text

from fleet_api.campaigns.actors.campaign_actor_models import OpenCycle


def sampled_delay(minimum: float, maximum: float) -> float:
    return minimum if minimum == maximum else random.uniform(minimum, maximum)


def observe_positions(
    service: LiveBetaVolumeService,
    lanes: Mapping[str, _Lane],
    round_number: int,
    *,
    action: str = "cycle_check",
) -> dict[str, Decimal | None]:
    observed = {
        symbol: service._observe_position(
            lane.venue,
            round_number=round_number,
            sequence="actor",
            symbol=symbol,
            action=action,
        )
        for symbol, lane in lanes.items()
    }
    positions: dict[str, Decimal | None] = {}
    for symbol, value in observed.items():
        if value is None:
            positions[symbol] = None
            continue
        try:
            quantity = Decimal(str(value))
        except (ArithmeticError, ValueError) as exc:
            raise SafetyError("position quantity observation is invalid") from exc
        if not quantity.is_finite():
            raise SafetyError("position quantity observation is invalid")
        positions[symbol] = quantity
    return positions


def targets_reached(
    positions: Mapping[str, Decimal | None],
    btc_plan: PairLegPlan,
    eth_plan: PairLegPlan,
) -> bool:
    expected = {
        "BTC": Decimal(str(_signed_open_quantity(btc_plan))),
        "ETH": Decimal(str(_signed_open_quantity(eth_plan))),
    }
    tolerances = {"BTC": btc_plan.amount_step / 2, "ETH": eth_plan.amount_step / 2}
    return all(
        positions[symbol] is not None and abs(positions[symbol] - expected[symbol]) <= tolerances[symbol]
        for symbol in ("BTC", "ETH")
    )


def positions_are_flat(
    positions: Mapping[str, Decimal | None],
    btc_plan: PairLegPlan,
    eth_plan: PairLegPlan,
) -> bool:
    tolerances = {"BTC": btc_plan.amount_step / 2, "ETH": eth_plan.amount_step / 2}
    return all(
        positions[symbol] is not None and abs(positions[symbol]) <= tolerances[symbol] for symbol in ("BTC", "ETH")
    )


def close_lanes(
    service: LiveBetaVolumeService,
    lanes: Mapping[str, _Lane],
    opened: OpenCycle,
    stops: dict[str, tuple[str, str]],
) -> list[dict[str, Any]]:
    jobs: dict[str, Any] = {}
    plans = {"BTC": opened.btc_plan, "ETH": opened.eth_plan}
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="fleet-close") as pool:
        for offset, symbol in enumerate(("BTC", "ETH"), 3):
            if stops.get(symbol, ("", ""))[0] == "submission_uncertain":
                continue
            position = service._observe_position(
                lanes[symbol].venue,
                round_number=opened.context.round_number,
                sequence="barrier",
                symbol=symbol,
                action="close",
            )
            if position is None:
                stops[symbol] = ("observation_uncertain", "position_observation_unavailable")
            elif abs(Decimal(str(position))) > plans[symbol].amount_step / 2:
                jobs[symbol] = pool.submit(
                    service._flatten_lane,
                    opened.plan,
                    opened.context.round_number,
                    offset,
                    plans[symbol],
                    lanes[symbol],
                    owned_quantity=_owned_position_quantity(
                        opened.open_summaries,
                        symbol,
                        plans[symbol].position_side,
                    ),
                )
        if jobs:
            service._emit("pair_waiting", round=opened.context.round_number, action="close", symbols=tuple(jobs))
        summaries: list[dict[str, Any]] = []
        for symbol in ("BTC", "ETH"):
            future = jobs.get(symbol)
            if future is None:
                continue
            rows, _, stop = future.result()
            summaries.extend(rows)
            if stop is not None:
                stops[symbol] = stop
    return summaries


def safe_stop(
    service: LiveBetaVolumeService,
    lanes: Mapping[str, _Lane],
    opened: OpenCycle,
) -> dict[str, Any]:
    """Use the emergency I/O stage to converge only the current cycle's legs."""
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="fleet-safe") as pool:
        return service._safe_stop(
            opened.plan,
            lanes,
            opened.preflight,
            opened.context.execution_started_at_ms,
            summaries=opened.context.summaries + opened.open_summaries,
            cycles=opened.context.cycles,
            total_quote=opened.context.child_total_quote,
            round_number=opened.context.round_number,
            pool=pool,
        )


def cycle_record(
    opened: OpenCycle,
    legs: list[dict[str, Any]],
    quote: Decimal,
    positions: Mapping[str, Decimal | None],
    *,
    flat: bool,
    reason: str | None,
    uncertain: bool,
    round_gap_seconds: float,
    elapsed_ms: int,
) -> dict[str, Any]:
    status = "uncertain" if uncertain else "stopped" if reason or not flat else "empty" if quote == 0 else "completed"
    open_summaries = {str(row.get("symbol")): row for row in opened.open_summaries}
    open_btc = Decimal(str(open_summaries.get("BTC", {}).get("quote_volume") or 0))
    open_eth = Decimal(str(open_summaries.get("ETH", {}).get("quote_volume") or 0))
    actual_beta = open_eth / open_btc if open_btc > 0 else None
    return {
        "round": opened.context.round_number,
        "status": status,
        "reason": reason or ("paired_cycle_flat" if flat else "paired_cycle_not_flat"),
        "desired_quote": opened.sizing.get("planned_turnover_quote", opened.sizing["opening_notional_quote"]),
        "executed_quote_volume": decimal_text(quote),
        "cumulative_quote_volume": decimal_text(opened.context.child_total_quote),
        "planned_open_beta": opened.sizing.get("planned_open_beta"),
        "actual_open_beta": decimal_text(actual_beta),
        "leverage": opened.selected_leverage,
        "leverage_state": opened.leverage_state,
        "hold_seconds": opened.hold_seconds,
        "round_gap_seconds": round_gap_seconds,
        "flat": flat,
        "positions": {key: None if value is None else decimal_text(value) for key, value in positions.items()},
        "accounting": _accounting_summary(legs),
        "elapsed_ms": elapsed_ms,
        "legs": legs,
    }
