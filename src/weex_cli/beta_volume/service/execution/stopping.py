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
    ExecutionLane,
)
from ...plan import BetaVolumePlan
from ...safety import (
    _row_count,
)


class SafetyStopMixin:
    def _safe_stop(
        self,
        plan: BetaVolumePlan,
        lanes: Mapping[str, ExecutionLane],
        preflight: Mapping[str, Any],
        execution_started_ms: int,
        *,
        summaries: list[dict[str, Any]],
        cycles: list[dict[str, Any]],
        total_quote: Decimal,
        round_number: int,
        pool: ThreadPoolExecutor | None = None,
    ) -> dict[str, Any]:
        """Cancel orders, flatten execution-owned positions, then prove the boundary.

        Maker remains the normal close path. A position-ID market close is allowed
        once only for a proven execution-owned rule dust remainder; no cancellation
        or market-close mutation is retried after an ambiguous response.
        """
        self._emit("safe_stop_started", round=round_number)
        cancellation_verified = True
        for symbol in ("BTC", "ETH"):
            cleanup = getattr(lanes[symbol].venue, "cancel_all_and_verify", None)
            if not callable(cleanup):
                cancellation_verified = False
                self._emit(
                    "safe_stop_cancel_unverified",
                    round=round_number,
                    symbol=symbol,
                    reason="cleanup_unavailable",
                )
                continue
            try:
                verified = bool(cleanup())
            except Exception as exc:  # noqa: BLE001 - a cancellation may have landed; fail closed
                verified = False
                self._emit(
                    "safe_stop_cancel_unverified",
                    round=round_number,
                    symbol=symbol,
                    reason=f"cleanup_exception:{type(exc).__name__.lower()}",
                )
            else:
                self._emit(
                    "safe_stop_cancel_verified" if verified else "safe_stop_cancel_unverified",
                    round=round_number,
                    symbol=symbol,
                )
            cancellation_verified = cancellation_verified and verified
        if not cancellation_verified:
            self._emit("safe_stop_uncertain", round=round_number, reason="safe_stop_order_cancellation_unverified")
            return self._finish(
                plan,
                "uncertain",
                "safe_stop_order_cancellation_unverified",
                summaries,
                cycles,
                total_quote,
                lanes,
                preflight,
                execution_started_ms,
            )

        jobs: dict[str, Future[tuple[list[dict[str, Any]], bool, tuple[str, str] | None]]] = {}
        owns_pool = pool is None
        active_pool = pool or ThreadPoolExecutor(max_workers=2, thread_name_prefix="weex-safe-stop")
        try:
            for offset, symbol in enumerate(("BTC", "ETH"), 1):
                leg_plan = plan.btc if symbol == "BTC" else plan.eth
                position = self._observe_position(
                    lanes[symbol].venue,
                    round_number=round_number,
                    sequence="safe-stop",
                    symbol=symbol,
                    action="safe_stop_check",
                )
                if position is None:
                    self._emit(
                        "safe_stop_uncertain",
                        round=round_number,
                        symbol=symbol,
                        reason="position_observation_unavailable",
                    )
                    return self._finish(
                        plan,
                        "uncertain",
                        "safe_stop_position_observation_unavailable",
                        summaries,
                        cycles,
                        total_quote,
                        lanes,
                        preflight,
                        execution_started_ms,
                    )
                if abs(Decimal(str(position))) <= leg_plan.amount_step / 2:
                    continue
                self._emit(
                    "safe_stop_flattening",
                    round=round_number,
                    symbol=symbol,
                    quantity=decimal_text(abs(Decimal(str(position)))),
                )
                jobs[symbol] = active_pool.submit(
                    self._flatten_lane,
                    plan,
                    round_number,
                    100 + offset,
                    leg_plan,
                    lanes[symbol],
                    respect_stop=False,
                    owned_quantity=owned_position_quantity(summaries, symbol, leg_plan.position_side),
                )
            for symbol in ("BTC", "ETH"):
                future = jobs.get(symbol)
                if future is None:
                    continue
                lane_summaries, flat, stop = future.result()
                summaries.extend(lane_summaries)
                audit_pending = stop is not None and stop[1] == "dust_close_audit_pending" and flat
                if not flat or (stop is not None and not audit_pending):
                    reason = stop[1] if stop is not None else "safe_stop_flatten_incomplete"
                    self._emit("safe_stop_uncertain", round=round_number, symbol=symbol, reason=reason)
                    return self._finish(
                        plan,
                        "uncertain",
                        reason,
                        summaries,
                        cycles,
                        total_quote,
                        lanes,
                        preflight,
                        execution_started_ms,
                    )
                self._emit(
                    "safe_stop_leg_completed",
                    round=round_number,
                    symbol=symbol,
                    audit_pending=audit_pending,
                )
        finally:
            if owns_pool:
                active_pool.shutdown(wait=True)

        positions = {
            symbol: self._observe_position(
                lane.venue,
                round_number=round_number,
                sequence="safe-stop-final",
                symbol=symbol,
                action="safe_stop_final_check",
            )
            for symbol, lane in lanes.items()
        }
        observations = {
            symbol: self._observe_orders(
                lane,
                round_number=round_number,
                sequence="safe-stop-final",
                symbol=symbol,
                action="safe_stop_final_check",
            )
            for symbol, lane in lanes.items()
        }
        flat = all(
            positions[symbol] is not None
            and abs(Decimal(str(positions[symbol]))) <= (plan.btc if symbol == "BTC" else plan.eth).amount_step / 2
            for symbol in ("BTC", "ETH")
        )
        no_orders = all(
            observation is not None and not observation[0] and _row_count(observation[1]) == 0
            for observation in observations.values()
        )
        if not flat or not no_orders:
            self._emit("safe_stop_uncertain", round=round_number, reason="safe_stop_final_boundary_unverified")
            return self._finish(
                plan,
                "uncertain",
                "safe_stop_final_boundary_unverified",
                summaries,
                cycles,
                total_quote,
                lanes,
                preflight,
                execution_started_ms,
            )
        self._emit("safe_stop_verified", round=round_number)
        return self._finish(
            plan,
            "stopped",
            "safe_stop_flattened",
            summaries,
            cycles,
            total_quote,
            lanes,
            preflight,
            execution_started_ms,
        )
