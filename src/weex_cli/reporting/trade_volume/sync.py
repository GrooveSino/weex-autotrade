"""Incremental Demo history synchronizer backed by the local ledger."""

from __future__ import annotations

from typing import Any

from weex_cli.core.errors import ValidationError
from weex_cli.core.symbols import demo_symbol_id

from .contracts import DEMO_PAGE_LIMIT, SyncState, TradeHistoryGateway, TradeVolumeRateLimited
from .ledger import SQLiteTradeVolumeLedger
from .support import normalize_demo_rows


class DemoTradeVolumeSyncService:
    def __init__(self, gateway: TradeHistoryGateway, ledger: SQLiteTradeVolumeLedger, account_id: str) -> None:
        self.gateway = gateway
        self.ledger = ledger
        self.account_id = account_id

    def sync(
        self,
        *,
        start_time: int,
        end_time: int,
        symbol: str | None = None,
        max_requests: int = 50,
        overlap_ms: int = 60_000,
    ) -> dict[str, Any]:
        if start_time < 0 or end_time < start_time:
            raise ValidationError("Invalid volume sync time range")
        if max_requests < 1:
            raise ValidationError("max_requests must be positive")
        symbol_key = demo_symbol_id(symbol) if symbol else "*"
        state = self.ledger.ensure_state(self.account_id, "demo", symbol_key, start_time, end_time)
        requests = inserted = updated = unchanged = 0
        rate_limited = False

        if not state.backfill_complete:
            while requests < max_requests:
                window = self.ledger.next_window(self.account_id, "demo", symbol_key)
                if window is None:
                    state = SyncState(
                        state.history_start_ms,
                        state.backfill_end_ms,
                        state.backfill_end_ms + 1,
                        0,
                        True,
                        state.backfill_end_ms,
                    )
                    self.ledger.save_state(self.account_id, "demo", symbol_key, state)
                    break
                window_start, window_end = window
                try:
                    batch = self._fetch(symbol, window_start, window_end, 0)
                except TradeVolumeRateLimited:
                    requests += 1
                    rate_limited = True
                    break
                else:
                    requests += 1
                if len(batch) >= DEMO_PAGE_LIMIT:
                    if window_start < window_end:
                        self.ledger.split_window(self.account_id, "demo", symbol_key, window_start, window_end)
                    else:
                        counts = self.ledger.record(self.account_id, "demo", normalize_demo_rows(batch))
                        inserted += counts[0]
                        updated += counts[1]
                        unchanged += counts[2]
                        self.ledger.mark_gap(self.account_id, "demo", symbol_key, window_start, window_end)
                else:
                    counts = self.ledger.record(self.account_id, "demo", normalize_demo_rows(batch))
                    inserted += counts[0]
                    updated += counts[1]
                    unchanged += counts[2]
                    self.ledger.finish_window(self.account_id, "demo", symbol_key, window_start, window_end)

        poll_complete = True
        if not rate_limited and state.backfill_complete and end_time > state.last_poll_ms and requests < max_requests:
            poll_start = max(state.history_start_ms, state.last_poll_ms - overlap_ms)
            pending = [(poll_start, end_time)]
            while pending and requests < max_requests:
                window_start, window_end = pending.pop()
                try:
                    batch = self._fetch(symbol, window_start, window_end, 0)
                except TradeVolumeRateLimited:
                    requests += 1
                    rate_limited = True
                    poll_complete = False
                    break
                else:
                    requests += 1
                if len(batch) >= DEMO_PAGE_LIMIT and window_start < window_end:
                    midpoint = (window_start + window_end) // 2
                    pending.extend(((window_start, midpoint), (midpoint + 1, window_end)))
                    continue
                counts = self.ledger.record(self.account_id, "demo", normalize_demo_rows(batch))
                inserted += counts[0]
                updated += counts[1]
                unchanged += counts[2]
                if len(batch) >= DEMO_PAGE_LIMIT:
                    self.ledger.mark_gap(self.account_id, "demo", symbol_key, window_start, window_end)
            if pending:
                poll_complete = False
            elif not rate_limited:
                state = SyncState(
                    state.history_start_ms,
                    state.backfill_end_ms,
                    state.cursor_ms,
                    0,
                    True,
                    end_time,
                )
                self.ledger.save_state(self.account_id, "demo", symbol_key, state)

        gaps = self.ledger.gap_count(self.account_id, "demo", symbol_key)
        complete = state.backfill_complete and poll_complete and not rate_limited and gaps == 0
        return {
            "status": "rate_limited" if rate_limited else "completed" if complete else "partial",
            "mode": "demo",
            "source": "demo_order_history_incremental_cache",
            "network_requests": requests,
            "inserted_trades": inserted,
            "updated_trades": updated,
            "unchanged_trades": unchanged,
            "history_complete": state.backfill_complete and gaps == 0,
            "ambiguous_windows": gaps,
            "coverage_start_time": state.history_start_ms,
            "last_sync_time": state.last_poll_ms,
            "retry_after_seconds": 10 if rate_limited else None,
            "summary": self.ledger.summary(self.account_id, "demo", None if symbol is None else symbol_key),
        }

    def _fetch(self, symbol: str | None, start_time: int, end_time: int, page: int) -> list[dict[str, Any]]:
        try:
            rows = self.gateway.trade_rows(
                "demo",
                symbol,
                start_time=start_time,
                end_time=end_time,
                limit=DEMO_PAGE_LIMIT,
                page=page,
            )
        except Exception as exc:
            text = str(exc).lower()
            if "-1003" in text or "too much request weight" in text:
                raise TradeVolumeRateLimited from exc
            raise
        if not isinstance(rows, list):
            raise ValidationError("Demo order history returned a non-list response")
        return rows


_DECIMAL_TOTAL_KEYS = (
    "total_quote",
    "opening_quote",
    "closing_quote",
    "unknown_action_quote",
    "maker_quote",
    "taker_quote",
    "unknown_liquidity_quote",
)
_COUNT_TOTAL_KEYS = ("trade_count", "maker_count", "taker_count", "unknown_liquidity_count")
_TOTAL_KEYS = (*_DECIMAL_TOTAL_KEYS, *_COUNT_TOTAL_KEYS)
