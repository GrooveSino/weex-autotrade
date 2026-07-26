"""Record a close phase once, or retain it for a confirmed Maker re-quote."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from weex_cli.control_api.exchange import decimal_text
from weex_cli.control_api.volume import is_uncertain_stop, terminal_reason

from fleet_api.campaigns.actors.campaign_actor_cycles import (
    close_lanes,
    cycle_record,
    observe_positions,
    owned_positions_match_cycle,
    positions_are_flat,
    sampled_delay,
)
from fleet_api.campaigns.actors.campaign_actor_models import Campaign, CloseCycle, OpenCycle
from fleet_api.campaigns.actors.campaign_actor_planning import (
    retry_cycle_condition,
    retry_owned_close_condition,
)
from fleet_api.campaigns.actors.targets.target_policy import campaign_completion_floor, emit_tolerance_acceptance


def close_cycle(
    service: Any,
    lanes: Mapping[str, Any],
    campaign: Campaign,
    opened: OpenCycle,
    *,
    close_lanes_fn: Any = close_lanes,
    observe_positions_fn: Any = observe_positions,
    flat_checker: Any = positions_are_flat,
    terminal_reason_fn: Any = terminal_reason,
) -> CloseCycle:
    """Close both legs, retaining confirmed owned exposure for a fresh Maker quote."""
    context = opened.context
    stops = _retryable_stops_cleared(opened.lane_stops)
    service._emit("close_barrier_started", round=context.round_number)
    opened.close_summaries.extend(close_lanes_fn(service, lanes, opened, stops))
    service._emit("pair_wait_completed", round=context.round_number, action="close")
    legs = opened.open_summaries + opened.close_summaries
    service._refresh_pending_accounting(context.round_number, legs, lanes, stops)
    positions = observe_positions_fn(service, lanes, context.round_number)
    flat = flat_checker(positions, opened.btc_plan, opened.eth_plan)
    reason = terminal_reason_fn(stops)
    uncertain = any(is_uncertain_stop(stop) for stop in stops.values())
    owned = owned_positions_match_cycle(positions, opened)
    close_condition = retry_owned_close_condition(reason, flat=flat, uncertain=uncertain, owned=owned)
    opened.lane_stops = stops
    if close_condition is not None:
        service._emit(
            "owned_close_maker_retry",
            round=context.round_number,
            reason=reason,
            action="close",
            symbols=tuple(symbol for symbol, value in positions.items() if value not in {None, Decimal(0)}),
        )
        return CloseCycle(Decimal(0), None, None, None, 0, close_condition=close_condition)
    quote = sum((Decimal(str(row.get("quote_volume") or 0)) for row in legs), Decimal(0))
    context.child_total_quote += quote
    context.summaries.extend(legs)
    retry_condition = retry_cycle_condition(reason, quote, flat, uncertain)
    record_reason = None if retry_condition is not None else reason
    completion_floor = campaign_completion_floor(context)
    gap = _round_gap(campaign, completion_floor, flat, record_reason, retry_condition, uncertain)
    record = cycle_record(
        opened,
        legs,
        quote,
        positions,
        flat=flat,
        reason=record_reason,
        uncertain=uncertain,
        round_gap_seconds=gap,
        elapsed_ms=max(0, service.now_ms() - opened.started_at_ms),
    )
    context.cycles.append(record)
    service._emit(
        "cycle_completed" if record["status"] in {"completed", "recovered", "empty"} else "cycle_stopped",
        round=context.round_number,
        status=record["status"],
        reason=record["reason"],
        quote_volume=decimal_text(quote),
        total_quote=decimal_text(context.child_total_quote),
        target_quote=decimal_text(context.child.target_turnover_quote),
        remaining_quote=decimal_text(max(context.child.target_turnover_quote - context.child_total_quote, Decimal(0))),
        elapsed_ms=record["elapsed_ms"],
    )
    if uncertain:
        return CloseCycle(quote, None, None, reason or "lane_execution_uncertain", 0)
    if completion_floor is not None:
        emit_tolerance_acceptance(service, context, completion_floor)
        result = service._final_acceptance(
            context.child,
            context.summaries,
            context.cycles,
            context.child_total_quote,
            lanes,
            opened.preflight,
            context.execution_started_at_ms,
            minimum_accepted_quote=completion_floor,
        )
        return CloseCycle(quote, result, None, None, 0)
    if retry_condition is not None:
        if quote > 0:
            context.round_number += 1
        return CloseCycle(quote, None, None, None, 0, condition=retry_condition)
    if reason is not None or not flat:
        return CloseCycle(quote, None, reason or "paired_cycle_not_flat", None, 0)
    context.round_number += 1
    gap_started_at_ms = service.now_ms()
    service._emit(
        "round_gap_started",
        round=context.round_number - 1,
        seconds=gap,
        started_at_ms=gap_started_at_ms,
        deadline_at_ms=gap_started_at_ms + int(gap * 1_000),
    )
    return CloseCycle(quote, None, None, None, gap, gap_started_at_ms)


def _retryable_stops_cleared(stops: Mapping[str, tuple[str, str]]) -> dict[str, tuple[str, str]]:
    retryable = {
        "post_only_rejected",
        "stale_price",
        "maximum_residence",
        "unknown_liquidity",
        "recovery_attempts_exhausted",
    }
    return {symbol: stop for symbol, stop in stops.items() if stop[1] not in retryable}


def _round_gap(
    campaign: Campaign,
    completion_floor: Decimal | None,
    flat: bool,
    reason: str | None,
    retry_condition: Any,
    uncertain: bool,
) -> float:
    if uncertain or reason is not None or not flat or retry_condition is not None or completion_floor is not None:
        return 0
    return sampled_delay(campaign.round_gap_min_seconds, campaign.round_gap_max_seconds)
