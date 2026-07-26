from __future__ import annotations

import math
from collections.abc import Mapping
from decimal import Decimal

from weex_cli.core.errors import SafetyError

from ...contracts import (
    DelaySelector,
    ExecutionLane,
    PairLegPlan,
)
from ...plan import BetaVolumePlan
from ...safety import (
    _row_count,
    signed_open_quantity,
)
from ...sizing import _mid_price


class ExecutionFlowMixin:
    def _hold_open_pair(
        self,
        round_number: int,
        lane_stops: dict[str, tuple[str, str]],
        lanes: Mapping[str, ExecutionLane],
        btc_plan: PairLegPlan,
        eth_plan: PairLegPlan,
    ) -> float:
        if lane_stops:
            return 0.0
        positions = {
            symbol: self._observe_position(
                lane.venue,
                round_number=round_number,
                sequence="hold",
                symbol=symbol,
                action="hold_check",
            )
            for symbol, lane in lanes.items()
        }
        if any(positions[symbol] is None for symbol in ("BTC", "ETH")):
            for symbol in ("BTC", "ETH"):
                if positions[symbol] is None:
                    lane_stops[symbol] = ("observation_uncertain", "position_observation_unavailable")
            return 0.0
        expected_positions = {
            "BTC": Decimal(str(signed_open_quantity(btc_plan))),
            "ETH": Decimal(str(signed_open_quantity(eth_plan))),
        }
        tolerances = {
            "BTC": btc_plan.amount_step / 2,
            "ETH": eth_plan.amount_step / 2,
        }
        targets_reached = all(
            abs(Decimal(str(positions[symbol])) - expected_positions[symbol]) <= tolerances[symbol]
            for symbol in ("BTC", "ETH")
        )
        if not targets_reached:
            self._emit("open_barrier_not_ready", round=round_number)
            return 0.0
        seconds = self._delay_seconds(self.hold_delay_seconds, round_number, 0.0)
        if seconds:
            self._emit("open_barrier_verified", round=round_number)
            self._emit("hold_started", round=round_number, seconds=seconds)
            self._wait_for_stop(seconds)
            if self.stop_requested():
                return seconds
            self._emit("hold_completed", round=round_number, seconds=seconds)
        return seconds

    def _close_phase_boundary_ready(
        self,
        plan: BetaVolumePlan,
        lanes: Mapping[str, ExecutionLane],
        round_number: int,
    ) -> bool:
        try:
            if self.provider is None:
                return False
            self._read_with_retry(
                self.provider.get,
                operation="close_beta_observation",
                round=round_number,
            )
            for symbol, lane in lanes.items():
                self._read_with_retry(
                    lambda lane=lane, symbol=symbol: _mid_price(lane.gateway, symbol),
                    operation="close_market_observation",
                    round=round_number,
                    symbol=symbol,
                )
                position = self._observe_position(
                    lane.venue,
                    round_number=round_number,
                    sequence="pacing-boundary",
                    symbol=symbol,
                    action="close",
                )
                orders = self._observe_orders(
                    lane,
                    round_number=round_number,
                    sequence="pacing-boundary",
                    symbol=symbol,
                    action="close",
                )
                if position is None or orders is None:
                    return False
                active_orders, trigger_orders = orders
                if active_orders or _row_count(trigger_orders):
                    return False
        except Exception:  # noqa: BLE001 - normal close falls through to unpaced safe-stop
            return False
        return True

    def _wait_for_stop(self, seconds: float) -> None:
        """Wait without making stop requests wait for a full hold/gap interval."""
        if seconds <= 0:
            return
        if not self._stop_callback_configured:
            self.sleep(seconds)
            return
        remaining = seconds
        while remaining > 0:
            if self.stop_requested():
                return
            delay = min(0.125, remaining)
            self.sleep(delay)
            remaining -= delay

    @staticmethod
    def _delay_seconds(selector: DelaySelector | None, round_number: int, fallback: float) -> float:
        seconds = fallback if selector is None else float(selector(round_number))
        if not math.isfinite(seconds) or seconds < 0:
            raise SafetyError("delay selector returned an invalid duration")
        return seconds
