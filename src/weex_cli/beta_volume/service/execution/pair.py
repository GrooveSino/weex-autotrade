from __future__ import annotations

import time
from collections.abc import Mapping
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from typing import Any

from ...contracts import (
    CycleLegSpec,
    ExecutionLane,
)
from ...plan import BetaVolumePlan


class PairExecutionMixin:
    def _run_pair(
        self,
        pool: ThreadPoolExecutor,
        plan: BetaVolumePlan,
        round_number: int,
        sequence_offset: int,
        specs: Mapping[str, CycleLegSpec],
        lanes: Mapping[str, ExecutionLane],
    ) -> dict[str, tuple[dict[str, Any], tuple[str, str] | None]]:
        futures = {
            symbol: pool.submit(
                self._execute_leg,
                plan,
                (round_number - 1) * (2 + plan.recovery_attempts * 2) + sequence_offset + index,
                spec,
                lanes[symbol],
                round_number,
                respect_stop=True,
            )
            for index, (symbol, spec) in enumerate(specs.items())
        }
        action = next(iter(specs.values())).action
        started = time.monotonic()
        deadline_seconds = float(plan.timeout_seconds)
        self._emit(
            "pair_waiting",
            round=round_number,
            action=action,
            symbols=tuple(futures),
            active_symbols=tuple(futures),
            completed_symbols=(),
            elapsed_ms=0,
            remaining_ms=int(deadline_seconds * 1000),
        )
        by_future = {future: symbol for symbol, future in futures.items()}
        pending = set(by_future)
        completed: set[str] = set()
        results: dict[str, tuple[dict[str, Any], tuple[str, str] | None]] = {}
        while pending:
            done, pending = wait(
                pending,
                timeout=self.PAIR_HEARTBEAT_SECONDS,
                return_when=FIRST_COMPLETED,
            )
            for future in done:
                symbol = by_future[future]
                results[symbol] = future.result()
                completed.add(symbol)
            elapsed_seconds = time.monotonic() - started
            if pending:
                self._emit(
                    "pair_wait_progress",
                    round=round_number,
                    action=action,
                    symbols=tuple(futures),
                    active_symbols=tuple(symbol for symbol in ("BTC", "ETH") if futures.get(symbol) in pending),
                    completed_symbols=tuple(symbol for symbol in ("BTC", "ETH") if symbol in completed),
                    elapsed_ms=int(elapsed_seconds * 1000),
                    remaining_ms=max(0, int((deadline_seconds - elapsed_seconds) * 1000)),
                )
        self._emit(
            "pair_wait_completed",
            round=round_number,
            action=action,
            completed_symbols=tuple(symbol for symbol in ("BTC", "ETH") if symbol in completed),
        )
        return {symbol: results[symbol] for symbol in ("BTC", "ETH") if symbol in results}
