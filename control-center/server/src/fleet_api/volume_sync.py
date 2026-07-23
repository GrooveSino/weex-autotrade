from __future__ import annotations

from datetime import UTC, datetime

from .volume_contracts import FillConflictError, TradeHistoryContext, TradeHistorySource, TradeHistorySyncResult, TradeVolumeLedger


class TradeHistorySynchronizer:
    def __init__(self, ledger: TradeVolumeLedger, *, page_size: int = 100, max_pages: int = 1000) -> None:
        if page_size < 1:
            raise ValueError("history page size must be at least 1")
        if max_pages < 1:
            raise ValueError("history max pages must be at least 1")
        self._ledger = ledger
        self._page_size = page_size
        self._max_pages = max_pages

    async def sync(
        self,
        instance_id: str,
        context: TradeHistoryContext,
        source: TradeHistorySource,
        *,
        today_start_ms: int,
        cursor: str | None = None,
        coverage_start_ms: int | None = None,
    ) -> TradeHistorySyncResult:
        if cursor is None:
            self._ledger.set_complete(instance_id, False)
        seen_cursors: set[str] = set()
        inserted = 0
        high_watermark_ms: int | None = None
        account_mode = getattr(context.instance.mode, "value", str(context.instance.mode)).lower()
        for page_number in range(1, self._max_pages + 1):
            try:
                page = await source.fetch_page(context, cursor=cursor, limit=self._page_size)
            except Exception:
                checkpoint = self._ledger.sync_checkpoint(instance_id, account_mode) or {}
                self._ledger.save_sync_checkpoint(
                    instance_id,
                    account_mode,
                    cursor=cursor,
                    high_watermark_ms=high_watermark_ms,
                    pending=True,
                    source_complete=False,
                    coverage_complete=bool(checkpoint.get("coverage_complete", False)),
                    stale=True,
                )
                self._ledger.refresh_sessions(
                    instance_id,
                    account_mode,
                    now_ms=int(datetime.now(UTC).timestamp() * 1000),
                    source_complete=False,
                    stale=True,
                    coverage_start_ms=coverage_start_ms,
                    high_watermark_ms=high_watermark_ms,
                )
                raise
            candidates = [value for value in (high_watermark_ms, page.high_watermark_ms) if value is not None]
            high_watermark_ms = max(candidates) if candidates else None
            if hasattr(self._ledger, "record_account_fills"):
                try:
                    inserted += self._ledger.record_account_fills(instance_id, account_mode, page.fills)
                except FillConflictError:
                    if hasattr(self._ledger, "mark_sessions_reconciliation"):
                        self._ledger.mark_sessions_reconciliation(instance_id, account_mode)
                    raise
            else:
                inserted += self._ledger.record(instance_id, page.fills)
            if page.next_cursor is None:
                if page.complete:
                    self._ledger.set_complete(instance_id, True)
                if hasattr(self._ledger, "save_sync_checkpoint"):
                    self._ledger.save_sync_checkpoint(
                        instance_id,
                        account_mode,
                        cursor=None,
                        high_watermark_ms=high_watermark_ms,
                        pending=False,
                        source_complete=page.complete,
                        coverage_complete=page.complete,
                        stale=not page.complete,
                    )
                if hasattr(self._ledger, "refresh_sessions"):
                    session_window_complete = (
                        page.window_complete if page.window_complete is not None else page.complete
                    )
                    self._ledger.refresh_sessions(
                        instance_id,
                        account_mode,
                        now_ms=int(datetime.now(UTC).timestamp() * 1000),
                        source_complete=session_window_complete,
                        stale=not session_window_complete,
                        coverage_start_ms=coverage_start_ms,
                        high_watermark_ms=high_watermark_ms,
                    )
                return TradeHistorySyncResult(
                    aggregate=self._ledger.aggregate(instance_id, today_start_ms),
                    pages_fetched=page_number,
                    fills_inserted=inserted,
                    stop_reason="history_exhausted" if page.complete else "source_incomplete",
                    next_cursor=None,
                )
            if page.next_cursor == cursor or page.next_cursor in seen_cursors:
                if hasattr(self._ledger, "save_sync_checkpoint"):
                    self._ledger.save_sync_checkpoint(
                        instance_id,
                        account_mode,
                        cursor=cursor,
                        high_watermark_ms=high_watermark_ms,
                        pending=True,
                        source_complete=False,
                        coverage_complete=page.complete,
                        stale=True,
                    )
                if hasattr(self._ledger, "refresh_sessions"):
                    self._ledger.refresh_sessions(
                        instance_id,
                        account_mode,
                        now_ms=int(datetime.now(UTC).timestamp() * 1000),
                        source_complete=False,
                        stale=True,
                        coverage_start_ms=coverage_start_ms,
                        high_watermark_ms=high_watermark_ms,
                    )
                return TradeHistorySyncResult(
                    aggregate=self._ledger.aggregate(instance_id, today_start_ms),
                    pages_fetched=page_number,
                    fills_inserted=inserted,
                    stop_reason="cursor_loop",
                    next_cursor=cursor,
                )
            seen_cursors.add(page.next_cursor)
            cursor = page.next_cursor
        if hasattr(self._ledger, "save_sync_checkpoint"):
            self._ledger.save_sync_checkpoint(
                instance_id,
                account_mode,
                cursor=cursor,
                high_watermark_ms=high_watermark_ms,
                pending=True,
                source_complete=False,
                coverage_complete=page.complete,
                stale=True,
            )
        if hasattr(self._ledger, "refresh_sessions"):
            self._ledger.refresh_sessions(
                instance_id,
                account_mode,
                now_ms=int(datetime.now(UTC).timestamp() * 1000),
                source_complete=False,
                stale=True,
                coverage_start_ms=coverage_start_ms,
                high_watermark_ms=high_watermark_ms,
            )
        return TradeHistorySyncResult(
            aggregate=self._ledger.aggregate(instance_id, today_start_ms),
            pages_fetched=self._max_pages,
            fills_inserted=inserted,
            stop_reason="page_budget_exhausted",
            next_cursor=cursor,
        )
