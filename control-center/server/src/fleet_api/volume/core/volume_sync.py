from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fleet_api.volume.core.volume_contracts import (
    FillConflictError,
    TradeHistoryContext,
    TradeHistorySource,
    TradeHistorySyncResult,
    TradeVolumeLedger,
)


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
        seen_cursors: set[str] = set()
        inserted = 0
        checkpoint = (
            self._ledger.sync_checkpoint(
                instance_id,
                getattr(context.instance.mode, "value", str(context.instance.mode)).lower(),
            )
            or {}
        )
        high_watermark_ms = _checkpoint_watermark(checkpoint)
        account_mode = getattr(context.instance.mode, "value", str(context.instance.mode)).lower()
        for page_number in range(1, self._max_pages + 1):
            result = await self.step(
                instance_id,
                context,
                source,
                today_start_ms=today_start_ms,
                cursor=cursor,
                coverage_start_ms=coverage_start_ms,
                high_watermark_ms=high_watermark_ms,
            )
            inserted += result.fills_inserted
            checkpoint = self._ledger.sync_checkpoint(instance_id, account_mode) or {}
            high_watermark_ms = _checkpoint_watermark(checkpoint)
            if result.next_cursor is None or result.stop_reason == "cursor_loop":
                return TradeHistorySyncResult(
                    aggregate=result.aggregate,
                    pages_fetched=page_number,
                    fills_inserted=inserted,
                    stop_reason=result.stop_reason,
                    next_cursor=result.next_cursor,
                )
            if result.next_cursor in seen_cursors:
                self._mark_cursor_loop(
                    instance_id, account_mode, result.next_cursor, high_watermark_ms, coverage_start_ms
                )
                return TradeHistorySyncResult(
                    aggregate=self._ledger.aggregate(instance_id, today_start_ms),
                    pages_fetched=page_number,
                    fills_inserted=inserted,
                    stop_reason="cursor_loop",
                    next_cursor=result.next_cursor,
                )
            seen_cursors.add(result.next_cursor)
            cursor = result.next_cursor
        self._save_checkpoint(
            instance_id,
            account_mode,
            cursor=cursor,
            high_watermark_ms=high_watermark_ms,
            pending=True,
            source_complete=False,
            coverage_complete=bool(checkpoint.get("coverage_complete", False)),
            stale=True,
            source=source,
        )
        self._refresh_sessions(instance_id, account_mode, coverage_start_ms, high_watermark_ms, False, True)
        return TradeHistorySyncResult(
            aggregate=self._ledger.aggregate(instance_id, today_start_ms),
            pages_fetched=self._max_pages,
            fills_inserted=inserted,
            stop_reason="page_budget_exhausted",
            next_cursor=cursor,
        )

    async def step(
        self,
        instance_id: str,
        context: TradeHistoryContext,
        source: TradeHistorySource,
        *,
        today_start_ms: int,
        cursor: str | None = None,
        coverage_start_ms: int | None = None,
        high_watermark_ms: int | None = None,
    ) -> TradeHistorySyncResult:
        """Persist exactly one source page for an executor scheduler turn."""
        account_mode = getattr(context.instance.mode, "value", str(context.instance.mode)).lower()
        checkpoint = self._ledger.sync_checkpoint(instance_id, account_mode) or {}
        if cursor is None and not checkpoint.get("scan_state"):
            self._ledger.set_complete(instance_id, False)
        try:
            page = await source.fetch_page(context, cursor=cursor, limit=self._page_size)
        except Exception:
            watermark = high_watermark_ms if high_watermark_ms is not None else _checkpoint_watermark(checkpoint)
            self._save_checkpoint(
                instance_id,
                account_mode,
                cursor=cursor,
                high_watermark_ms=watermark,
                pending=True,
                source_complete=False,
                coverage_complete=bool(checkpoint.get("coverage_complete", False)),
                stale=True,
                source=source,
            )
            self._refresh_sessions(instance_id, account_mode, coverage_start_ms, watermark, False, True)
            raise
        watermark = _max_watermark(
            high_watermark_ms,
            _checkpoint_watermark(checkpoint),
            page.high_watermark_ms,
        )
        inserted = self._record_fills(instance_id, account_mode, page.fills)
        cursor_loop = page.next_cursor is not None and page.next_cursor == cursor
        complete = page.next_cursor is None and page.complete
        source_complete = (
            page.window_complete if page.next_cursor is None and page.window_complete is not None else complete
        )
        stop_reason = _step_stop_reason(cursor_loop, complete, page.next_cursor)
        next_cursor = cursor if cursor_loop else page.next_cursor
        self._save_checkpoint(
            instance_id,
            account_mode,
            cursor=next_cursor,
            high_watermark_ms=watermark,
            pending=next_cursor is not None,
            source_complete=complete,
            coverage_complete=complete,
            stale=cursor_loop or (page.next_cursor is None and not complete),
            source=source,
        )
        if complete:
            self._ledger.set_complete(instance_id, True)
        self._refresh_sessions(
            instance_id,
            account_mode,
            coverage_start_ms,
            watermark,
            source_complete,
            cursor_loop or not source_complete,
        )
        return TradeHistorySyncResult(
            aggregate=self._ledger.aggregate(instance_id, today_start_ms),
            pages_fetched=1,
            fills_inserted=inserted,
            stop_reason=stop_reason,
            next_cursor=next_cursor,
        )

    def _record_fills(self, instance_id: str, mode: str, fills: tuple[Any, ...]) -> int:
        try:
            return self._ledger.record_account_fills(instance_id, mode, fills)
        except FillConflictError:
            self._ledger.mark_sessions_reconciliation(instance_id, mode)
            raise

    def _mark_cursor_loop(
        self, instance_id: str, mode: str, cursor: str, high_watermark_ms: int | None, coverage_start_ms: int | None
    ) -> None:
        self._save_checkpoint(
            instance_id,
            mode,
            cursor=cursor,
            high_watermark_ms=high_watermark_ms,
            pending=True,
            source_complete=False,
            coverage_complete=False,
            stale=True,
            source=None,
        )
        self._refresh_sessions(instance_id, mode, coverage_start_ms, high_watermark_ms, False, True)

    def _save_checkpoint(
        self,
        instance_id: str,
        mode: str,
        *,
        cursor: str | None,
        high_watermark_ms: int | None,
        pending: bool,
        source_complete: bool,
        coverage_complete: bool,
        stale: bool,
        source: TradeHistorySource | None,
    ) -> None:
        scan_state = _source_state(source)
        self._ledger.save_sync_checkpoint(
            instance_id,
            mode,
            cursor=cursor,
            high_watermark_ms=high_watermark_ms,
            pending=pending,
            source_complete=source_complete,
            coverage_complete=coverage_complete,
            stale=stale,
            **({"scan_state": scan_state} if scan_state is not None else {}),
        )

    def _refresh_sessions(
        self,
        instance_id: str,
        mode: str,
        coverage_start_ms: int | None,
        high_watermark_ms: int | None,
        source_complete: bool,
        stale: bool,
    ) -> None:
        self._ledger.refresh_sessions(
            instance_id,
            mode,
            now_ms=int(datetime.now(UTC).timestamp() * 1000),
            source_complete=source_complete,
            stale=stale,
            coverage_start_ms=coverage_start_ms,
            high_watermark_ms=high_watermark_ms,
        )


def _checkpoint_watermark(checkpoint: dict[str, object]) -> int | None:
    value = checkpoint.get("high_watermark_ms")
    return value if isinstance(value, int) else None


def _max_watermark(*values: int | None) -> int | None:
    populated = [value for value in values if value is not None]
    return max(populated) if populated else None


def _step_stop_reason(cursor_loop: bool, complete: bool, next_cursor: str | None) -> str:
    if cursor_loop:
        return "cursor_loop"
    if complete:
        return "history_exhausted"
    return "source_incomplete" if next_cursor is None else "page_step"


def _source_state(source: TradeHistorySource | None) -> object | None:
    snapshot = getattr(source, "snapshot", None)
    return snapshot() if callable(snapshot) else None
