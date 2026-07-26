from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from weex_cli.core.models import decimal_text

from ...accounting.fills import accounting_summary
from ...accounting.payload import _result_payload
from ...accounting.termination import is_uncertain_stop, terminal_reason
from ...contracts import ExecutionLane
from ...plan import BetaVolumePlan


@dataclass(frozen=True)
class CycleCheckpoint:
    total_quote: Decimal
    cycle_quote: Decimal
    flat: bool
    uncertain: bool
    hard_reason: str | None
    empty_rounds: int
    round_gap_seconds: float


class CycleCheckpointMixin:
    def _stop_cycle(
        self,
        plan: BetaVolumePlan,
        lanes: Mapping[str, ExecutionLane],
        preflight: Mapping[str, Any],
        execution_started_ms: int,
        summaries: list[dict[str, Any]],
        cycles: list[dict[str, Any]],
        total_quote: Decimal,
        round_number: int,
        pool: Any,
    ) -> dict[str, Any]:
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

    def _checkpoint_cycle(
        self,
        plan: BetaVolumePlan,
        lanes: Mapping[str, ExecutionLane],
        preflight: Mapping[str, Any],
        execution_started_ms: int,
        summaries: list[dict[str, Any]],
        cycles: list[dict[str, Any]],
        total_quote: Decimal,
        empty_rounds: int,
        round_number: int,
        cycle_started_ms: int,
        desired_quote: Decimal,
        sizing: Mapping[str, Any],
        selected_leverage: int,
        leverage_state: Mapping[str, Any],
        hold_seconds: float,
        open_summaries: list[dict[str, Any]],
        close_summaries: list[dict[str, Any]],
        lane_stops: Mapping[str, tuple[str, str]],
        positions: Mapping[str, object],
        flat: bool,
    ) -> CycleCheckpoint:
        cycle_legs = open_summaries + close_summaries
        cycle_quote = sum((Decimal(str(row.get("quote_volume") or 0)) for row in cycle_legs), Decimal(0))
        total_quote += cycle_quote
        open_btc_quote = Decimal(str(open_summaries[0].get("quote_volume") or 0))
        open_eth_quote = Decimal(str(open_summaries[1].get("quote_volume") or 0))
        actual_beta = open_eth_quote / open_btc_quote if open_btc_quote > 0 else None
        cycle_accounting = accounting_summary(cycle_legs)
        uncertain = any(is_uncertain_stop(stop) for stop in lane_stops.values())
        hard_reason = terminal_reason(lane_stops)
        if uncertain:
            cycle_status = "uncertain"
        elif not flat:
            cycle_status = "stopped"
            hard_reason = hard_reason or "paired_cycle_not_flat"
        elif hard_reason is not None:
            cycle_status = "stopped"
        elif cycle_quote == 0:
            cycle_status = "empty"
        elif lane_stops:
            cycle_status = "recovered"
        else:
            cycle_status = "completed"
        next_empty_rounds = empty_rounds + 1 if cycle_quote == 0 else 0
        safe_to_continue = (
            not uncertain
            and hard_reason is None
            and flat
            and next_empty_rounds <= plan.max_empty_rounds
            and total_quote < plan.target_turnover_quote
        )
        round_gap_seconds = (
            self._delay_seconds(self.round_gap_delay_seconds, round_number, plan.cooldown_seconds)
            if safe_to_continue
            else 0.0
        )
        cycle = {
            "round": round_number,
            "status": cycle_status,
            "reason": hard_reason or ("paired_cycle_flat" if flat else "paired_cycle_not_flat"),
            "desired_quote": decimal_text(desired_quote),
            "executed_quote_volume": decimal_text(cycle_quote),
            "cumulative_quote_volume": decimal_text(total_quote),
            "planned_open_beta": sizing["planned_open_beta"],
            "actual_open_beta": decimal_text(actual_beta),
            "leverage": selected_leverage,
            "leverage_state": leverage_state,
            "hold_seconds": hold_seconds,
            "round_gap_seconds": round_gap_seconds,
            "flat": flat,
            "positions": positions,
            "accounting": cycle_accounting,
            "elapsed_ms": self.now_ms() - cycle_started_ms,
            "legs": cycle_legs,
        }
        cycles.append(cycle)
        self._emit(
            "cycle_completed" if cycle_status in {"completed", "recovered"} else "cycle_stopped",
            round=round_number,
            status=cycle_status,
            reason=cycle["reason"],
            quote_volume=decimal_text(cycle_quote),
            total_quote=decimal_text(total_quote),
            elapsed_ms=cycle["elapsed_ms"],
        )
        self.store.save(
            plan,
            state="executing",
            result=_result_payload(
                plan,
                "executing",
                "cycle_checkpointed",
                summaries,
                cycles,
                total_quote,
                {symbol: lane.venue for symbol, lane in lanes.items()},
                preflight,
                self.timeline,
                self.now_ms() - execution_started_ms,
            ),
        )
        return CycleCheckpoint(
            total_quote=total_quote,
            cycle_quote=cycle_quote,
            flat=flat,
            uncertain=uncertain,
            hard_reason=hard_reason,
            empty_rounds=next_empty_rounds,
            round_gap_seconds=round_gap_seconds,
        )
