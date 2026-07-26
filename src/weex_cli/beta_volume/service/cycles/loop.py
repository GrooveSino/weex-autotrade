from __future__ import annotations

from collections.abc import Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from decimal import Decimal
from typing import Any

from weex_cli.core.models import decimal_text

from ...accounting.fills import (
    owned_position_quantity,
)
from ...contracts import (
    CycleLegSpec,
    ExecutionLane,
)
from ...plan import BetaVolumePlan
from ...safety import (
    _cycle_leverage_failure_reason,
    signed_open_quantity,
)
from ...sizing import size_cycle


class CycleLoopMixin:
    def _execute_cycles(
        self,
        plan: BetaVolumePlan,
        lanes: Mapping[str, ExecutionLane],
        preflight: Mapping[str, Any],
        execution_started_ms: int,
    ) -> dict[str, Any]:
        summaries: list[dict[str, Any]] = []
        cycles: list[dict[str, Any]] = []
        total_quote = Decimal(0)
        empty_rounds = 0
        max_rounds = plan.estimated_rounds * 3 + plan.max_empty_rounds + 5

        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="weex-beta") as pool:
            for round_number in range(1, max_rounds + 1):
                if self.stop_requested():
                    return self._stop_cycle(
                        plan, lanes, preflight, execution_started_ms, summaries, cycles, total_quote, round_number, pool
                    )
                if total_quote >= plan.target_turnover_quote:
                    break
                if self.phase_waiter is not None and not self.phase_waiter(plan.plan_id, "open", round_number):
                    return self._stop_cycle(
                        plan, lanes, preflight, execution_started_ms, summaries, cycles, total_quote, round_number, pool
                    )
                if self.phase_waiter is not None:
                    preflight = self._preflight_with_read_retry(plan)
                desired_quote = min(plan.round_turnover_quote, plan.target_turnover_quote - total_quote)
                self._emit(
                    "cycle_preparing",
                    round=round_number,
                    desired_quote=decimal_text(desired_quote),
                )
                try:
                    btc_plan, eth_plan, sizing = self._read_with_retry(
                        lambda desired_quote=desired_quote: size_cycle(plan, lanes, desired_quote),
                        operation="cycle_sizing",
                        retry_event="cycle_sizing_retry",
                        round=round_number,
                    )
                except Exception as exc:  # noqa: BLE001 - sizing happens only at a proven flat boundary
                    return self._finish(
                        plan,
                        "stopped",
                        f"cycle_sizing:{type(exc).__name__.lower()}",
                        summaries,
                        cycles,
                        total_quote,
                        lanes,
                        preflight,
                        execution_started_ms,
                    )
                self._emit(
                    "leverage_preparing",
                    round=round_number,
                    opening_notional_quote=sizing["opening_notional_quote"],
                )
                try:
                    selected_leverage, leverage_state = self._prepare_cycle_leverage(
                        plan,
                        Decimal(sizing["opening_notional_quote"]),
                        round_number,
                    )
                except Exception as exc:  # noqa: BLE001 - no order is submitted until leverage is proven
                    reason = _cycle_leverage_failure_reason(exc)
                    self._emit("cycle_stopped", round=round_number, status="stopped", reason=reason)
                    return self._finish(
                        plan,
                        "stopped",
                        reason,
                        summaries,
                        cycles,
                        total_quote,
                        lanes,
                        preflight,
                        execution_started_ms,
                    )
                self._emit(
                    "cycle_started",
                    round=round_number,
                    desired_quote=decimal_text(desired_quote),
                    btc_quantity=decimal_text(btc_plan.quantity),
                    eth_quantity=decimal_text(eth_plan.quantity),
                    leverage=selected_leverage,
                )
                cycle_started_ms = self.now_ms()
                open_specs = {
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
                open_results = self._run_pair(pool, plan, round_number, 1, open_specs, lanes)
                open_summaries = [open_results[symbol][0] for symbol in ("BTC", "ETH")]
                summaries.extend(open_summaries)

                if self.stop_requested():
                    return self._stop_cycle(
                        plan, lanes, preflight, execution_started_ms, summaries, cycles, total_quote, round_number, pool
                    )

                lane_stops: dict[str, tuple[str, str]] = {
                    symbol: result[1] for symbol, result in open_results.items() if result[1] is not None
                }
                hold_seconds = self._hold_open_pair(
                    round_number,
                    lane_stops,
                    lanes,
                    btc_plan,
                    eth_plan,
                )
                if self.stop_requested():
                    return self._stop_cycle(
                        plan, lanes, preflight, execution_started_ms, summaries, cycles, total_quote, round_number, pool
                    )
                if self.phase_waiter is not None and not self.phase_waiter(plan.plan_id, "close", round_number):
                    return self._stop_cycle(
                        plan, lanes, preflight, execution_started_ms, summaries, cycles, total_quote, round_number, pool
                    )
                if self.phase_waiter is not None and not self._close_phase_boundary_ready(plan, lanes, round_number):
                    return self._stop_cycle(
                        plan, lanes, preflight, execution_started_ms, summaries, cycles, total_quote, round_number, pool
                    )
                close_futures: dict[str, Future[tuple[list[dict[str, Any]], bool, tuple[str, str] | None]]] = {}
                self._emit("close_barrier_started", round=round_number)
                for offset, symbol in enumerate(("BTC", "ETH"), 3):
                    stop = lane_stops.get(symbol)
                    if stop is not None and stop[0] == "submission_uncertain":
                        continue
                    position = self._observe_position(
                        lanes[symbol].venue,
                        round_number=round_number,
                        sequence="barrier",
                        symbol=symbol,
                        action="close",
                    )
                    if position is None:
                        lane_stops[symbol] = ("observation_uncertain", "position_observation_unavailable")
                        continue
                    leg_plan = btc_plan if symbol == "BTC" else eth_plan
                    if abs(Decimal(str(position))) <= leg_plan.amount_step / 2:
                        continue
                    close_futures[symbol] = pool.submit(
                        self._flatten_lane,
                        plan,
                        round_number,
                        offset,
                        leg_plan,
                        lanes[symbol],
                        owned_quantity=owned_position_quantity(open_summaries, symbol, leg_plan.position_side),
                    )

                close_summaries: list[dict[str, Any]] = []
                if close_futures:
                    self._emit(
                        "pair_waiting",
                        round=round_number,
                        action="close",
                        symbols=tuple(close_futures),
                    )
                for symbol in ("BTC", "ETH"):
                    future = close_futures.get(symbol)
                    if future is None:
                        continue
                    lane_summaries, _, close_stop = future.result()
                    close_summaries.extend(lane_summaries)
                    if close_stop is not None:
                        lane_stops[symbol] = close_stop
                self._emit("pair_wait_completed", round=round_number, action="close")
                summaries.extend(close_summaries)

                if self.stop_requested():
                    return self._safe_stop(
                        plan,
                        lanes,
                        preflight,
                        execution_started_ms,
                        summaries=summaries,
                        cycles=cycles,
                        total_quote=total_quote,
                        round_number=round_number,
                        pool=pool,
                    )

                cycle_legs = open_summaries + close_summaries
                self._refresh_pending_accounting(round_number, cycle_legs, lanes, lane_stops)
                positions = {
                    symbol: self._observe_position(
                        lane.venue,
                        round_number=round_number,
                        sequence="checkpoint",
                        symbol=symbol,
                        action="cycle_check",
                    )
                    for symbol, lane in lanes.items()
                }
                flat = all(
                    positions[symbol] is not None
                    and abs(Decimal(str(positions[symbol])))
                    <= (btc_plan.amount_step if symbol == "BTC" else eth_plan.amount_step) / 2
                    for symbol in ("BTC", "ETH")
                )
                checkpoint = self._checkpoint_cycle(
                    plan,
                    lanes,
                    preflight,
                    execution_started_ms,
                    summaries,
                    cycles,
                    total_quote,
                    empty_rounds,
                    round_number,
                    cycle_started_ms,
                    desired_quote,
                    sizing,
                    selected_leverage,
                    leverage_state,
                    hold_seconds,
                    open_summaries,
                    close_summaries,
                    lane_stops,
                    positions,
                    flat,
                )
                total_quote = checkpoint.total_quote
                empty_rounds = checkpoint.empty_rounds

                if checkpoint.uncertain:
                    return self._finish(
                        plan,
                        "uncertain",
                        checkpoint.hard_reason or "lane_execution_uncertain",
                        summaries,
                        cycles,
                        total_quote,
                        lanes,
                        preflight,
                        execution_started_ms,
                    )
                if checkpoint.hard_reason is not None or not checkpoint.flat:
                    return self._finish(
                        plan,
                        "stopped",
                        checkpoint.hard_reason or "paired_cycle_not_flat",
                        summaries,
                        cycles,
                        total_quote,
                        lanes,
                        preflight,
                        execution_started_ms,
                    )
                if checkpoint.cycle_quote == 0 and checkpoint.empty_rounds > plan.max_empty_rounds:
                    return self._finish(
                        plan,
                        "stopped",
                        "empty_round_limit_exhausted",
                        summaries,
                        cycles,
                        total_quote,
                        lanes,
                        preflight,
                        execution_started_ms,
                    )
                if total_quote < plan.target_turnover_quote and checkpoint.round_gap_seconds:
                    self._emit("round_gap_started", round=round_number, seconds=checkpoint.round_gap_seconds)
                    self._wait_for_stop(checkpoint.round_gap_seconds)
                    if self.stop_requested():
                        return self._stop_cycle(
                            plan,
                            lanes,
                            preflight,
                            execution_started_ms,
                            summaries,
                            cycles,
                            total_quote,
                            round_number,
                            pool,
                        )
                    self._emit("round_gap_completed", round=round_number, seconds=checkpoint.round_gap_seconds)

        if total_quote < plan.target_turnover_quote:
            return self._finish(
                plan,
                "stopped",
                "round_limit_exhausted",
                summaries,
                cycles,
                total_quote,
                lanes,
                preflight,
                execution_started_ms,
            )
        return self._final_acceptance(plan, summaries, cycles, total_quote, lanes, preflight, execution_started_ms)
