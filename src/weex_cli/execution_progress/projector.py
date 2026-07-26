"""Canonical execution-progress projector."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
from decimal import Decimal
from typing import Any

from .contracts import (
    EXECUTION_PROGRESS_PROJECTION_VERSION,
    ActiveWait,
    TimelinePresentation,
    event_name,
    event_value,
    execution_phase,
)
from .helpers import _decimal_or_zero, _nonnegative_decimal, _nonnegative_int
from .timeline import describe_execution_event
from .waits import ExecutionProgressWaitMixin


class ExecutionProgressProjector(ExecutionProgressWaitMixin):
    def __init__(self) -> None:
        self.active_waits: dict[str, ActiveWait] = {}
        self.phase = "启动"
        self.current_run = 0
        self.current_round = 0
        self.submissions = 0
        self.cancels = 0
        self.requotes = 0
        self.execution_verified_quote_volume = Decimal(0)
        self.btc_quote_volume = Decimal(0)
        self.eth_quote_volume = Decimal(0)
        self.execution_unknown_fill_count = 0
        self.condition_state: str | None = None
        self.condition_attempt = 0
        self.next_condition_check_at_ms: int | None = None
        self._current_run_base_quote = Decimal(0)
        self._completed_leg_quotes: dict[str, Decimal] = {}
        self._completed_leg_fill_counts: dict[str, int] = {}
        self._terminal_rounds: set[int] = set()

    def apply(self, event: Mapping[str, Any], *, at_ms: int) -> TimelinePresentation | None:
        phase = execution_phase(event)
        if phase is not None and not self._is_stale_round_wait(event):
            self.phase = phase
        run = event_value(event, "run")
        round_number = event_value(event, "round")
        if run is not None:
            self.current_run = max(self.current_run, int(run))
        if round_number is not None:
            self.current_round = max(self.current_round, int(round_number))
        self._update_volume(event)
        consumed = self._update_waits(event, at_ms)
        self._update_counts(event)
        return None if consumed else describe_execution_event(event)

    def snapshot(self) -> dict[str, Any]:
        return {
            "schema_version": EXECUTION_PROGRESS_PROJECTION_VERSION,
            "phase": self.phase,
            "current_run": self.current_run,
            "current_round": self.current_round,
            "submissions": self.submissions,
            "cancels": self.cancels,
            "requotes": self.requotes,
            "execution_verified_quote_volume": format(self.execution_verified_quote_volume, "f"),
            "btc_quote_volume": format(self.btc_quote_volume, "f"),
            "eth_quote_volume": format(self.eth_quote_volume, "f"),
            "execution_unknown_fill_count": self.execution_unknown_fill_count,
            "condition_state": self.condition_state,
            "condition_attempt": self.condition_attempt,
            "next_condition_check_at_ms": self.next_condition_check_at_ms,
            "active_waits": [asdict(wait) for wait in self.active_waits.values()],
            "current_run_base_quote": format(self._current_run_base_quote, "f"),
            "completed_leg_quotes": {key: format(value, "f") for key, value in self._completed_leg_quotes.items()},
            "completed_leg_fill_counts": self._completed_leg_fill_counts,
            "terminal_rounds": sorted(self._terminal_rounds),
        }

    @classmethod
    def from_snapshot(cls, snapshot: Mapping[str, Any] | None) -> ExecutionProgressProjector:
        projector = cls()
        if not isinstance(snapshot, Mapping):
            return projector
        projector.phase = str(snapshot.get("phase") or projector.phase)
        projector.current_run = _nonnegative_int(snapshot.get("current_run"))
        projector.current_round = _nonnegative_int(snapshot.get("current_round"))
        projector.submissions = _nonnegative_int(snapshot.get("submissions"))
        projector.cancels = _nonnegative_int(snapshot.get("cancels"))
        projector.requotes = _nonnegative_int(snapshot.get("requotes"))
        projector.execution_verified_quote_volume = _decimal_or_zero(snapshot.get("execution_verified_quote_volume"))
        projector.btc_quote_volume = _decimal_or_zero(snapshot.get("btc_quote_volume"))
        projector.eth_quote_volume = _decimal_or_zero(snapshot.get("eth_quote_volume"))
        projector.execution_unknown_fill_count = _nonnegative_int(snapshot.get("execution_unknown_fill_count"))
        state = snapshot.get("condition_state")
        projector.condition_state = str(state) if isinstance(state, str) and state else None
        projector.condition_attempt = _nonnegative_int(snapshot.get("condition_attempt"))
        next_check = snapshot.get("next_condition_check_at_ms")
        projector.next_condition_check_at_ms = _nonnegative_int(next_check) or None
        projector._current_run_base_quote = _decimal_or_zero(snapshot.get("current_run_base_quote"))
        completed = snapshot.get("completed_leg_quotes")
        if isinstance(completed, Mapping):
            projector._completed_leg_quotes = {
                str(key): parsed
                for key, value in completed.items()
                if (parsed := _nonnegative_decimal(value)) is not None
            }
        fill_counts = snapshot.get("completed_leg_fill_counts")
        if isinstance(fill_counts, Mapping):
            projector._completed_leg_fill_counts = {
                str(key): _nonnegative_int(value) for key, value in fill_counts.items()
            }
        terminal_rounds = snapshot.get("terminal_rounds")
        if isinstance(terminal_rounds, list):
            projector._terminal_rounds = {
                parsed for value in terminal_rounds if (parsed := _nonnegative_int(value)) > 0
            }
        waits = snapshot.get("active_waits")
        if isinstance(waits, list):
            for raw in waits:
                if not isinstance(raw, Mapping) or not raw.get("key"):
                    continue
                try:
                    wait = ActiveWait(
                        key=str(raw["key"]),
                        label=str(raw.get("label") or raw["key"]),
                        updated_at_ms=int(raw.get("updated_at_ms") or 0),
                        elapsed_ms=_nonnegative_int(raw.get("elapsed_ms")),
                        remaining_ms=(
                            None if raw.get("remaining_ms") is None else _nonnegative_int(raw.get("remaining_ms"))
                        ),
                        detail=str(raw.get("detail") or ""),
                        symbol=str(raw["symbol"]) if raw.get("symbol") else None,
                        action=str(raw["action"]) if raw.get("action") else None,
                        started_at_ms=(None if raw.get("started_at_ms") is None else int(raw["started_at_ms"])),
                        deadline_at_ms=(None if raw.get("deadline_at_ms") is None else int(raw["deadline_at_ms"])),
                    )
                except (TypeError, ValueError):
                    continue
                projector.active_waits[wait.key] = wait
        return projector

    def _update_volume(self, event: Mapping[str, Any]) -> None:
        name = event_name(event)
        if name == "campaign_run_started":
            self._current_run_base_quote = self.execution_verified_quote_volume
            return
        if name in {"cycle_completed", "cycle_stopped"}:
            status = str(event_value(event, "status", ""))
            # Modern executions persist one leg_completed event per reconciled
            # fill batch.  Only use an aggregate cycle total to recover old
            # journals that genuinely have no leg-level evidence.
            if status not in {"completed", "recovered"} or self._completed_leg_quotes:
                return
            child_total = _nonnegative_decimal(event_value(event, "total_quote"))
            if child_total is not None:
                self.execution_verified_quote_volume = max(
                    self.execution_verified_quote_volume,
                    self._current_run_base_quote + child_total,
                )
            return
        if name in {"campaign_run_completed", "campaign_finished"}:
            total = _nonnegative_decimal(event_value(event, "total_quote"))
            if total is not None:
                self.execution_verified_quote_volume = max(self.execution_verified_quote_volume, total)
                self._current_run_base_quote = self.execution_verified_quote_volume
            return
        if name not in {"leg_completed", "market_close_verified"}:
            return
        if name == "market_close_verified" and not bool(event_value(event, "verified", False)):
            return
        quote = _nonnegative_decimal(event_value(event, "quote_volume"))
        if quote is None:
            return
        symbol = str(event_value(event, "symbol", "")).upper()
        round_number = event_value(event, "round", "")
        leg_sequence = event_value(
            event,
            "leg_sequence",
            event_value(event, "sequence", "dust" if name == "market_close_verified" else ""),
        )
        action = str(event_value(event, "action", ""))
        key = f"{self.current_run}:{round_number}:{leg_sequence}:{symbol}:{action}"
        previous = self._completed_leg_quotes.get(key)
        fill_count = _nonnegative_int(event_value(event, "fill_count"))
        previous_fill_count = self._completed_leg_fill_counts.get(key, 0)
        if previous == quote and previous_fill_count == fill_count:
            return
        # A leg_completed event is emitted only after the maker execution
        # service has reconciled actual fills for that leg.  Keep this
        # execution-journal total live while the independent fill ledger is
        # catching up; planned cycle values never enter this path.
        self.execution_verified_quote_volume += quote - (previous or Decimal(0))
        if symbol.startswith("BTC"):
            self.btc_quote_volume += quote - (previous or Decimal(0))
        elif symbol.startswith("ETH"):
            self.eth_quote_volume += quote - (previous or Decimal(0))
        self._completed_leg_quotes[key] = quote
        # The journal confirms the count but deliberately does not retain
        # individual fill identities or maker classification.  Until the
        # independent ledger catches up, make that uncertainty visible rather
        # than presenting a misleading all-zero breakdown.
        self.execution_unknown_fill_count += fill_count - previous_fill_count
        self._completed_leg_fill_counts[key] = fill_count

    def _update_counts(self, event: Mapping[str, Any]) -> None:
        if event_name(event) != "leg_progress":
            return
        progress = str(event_value(event, "progress_event", ""))
        if progress == "submit":
            self.submissions += 1
        elif progress == "cancel":
            self.cancels += 1
            self.requotes += 1
