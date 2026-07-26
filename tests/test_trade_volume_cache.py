from __future__ import annotations

from pathlib import Path

from weex_cli.reporting.trade_volume import (
    DEMO_PAGE_LIMIT,
    DemoTradeVolumeSyncService,
    SQLiteTradeVolumeLedger,
    account_fingerprint,
)


def order(
    order_id: str,
    *,
    timestamp: int,
    quote: str,
    side: str = "BUY",
    position_side: str = "LONG",
    tif: str = "POST_ONLY",
) -> dict[str, object]:
    return {
        "orderId": order_id,
        "symbol": "BTCSUSDT",
        "executedQty": "0.01",
        "avgPrice": "70000",
        "cumQuote": quote,
        "updateTime": timestamp,
        "side": side,
        "positionSide": position_side,
        "timeInForce": tif,
    }


class HistoryGateway:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows
        self.calls: list[dict[str, int | str | None]] = []

    def trade_rows(self, mode, symbol, *, start_time, end_time, limit, page=None):
        self.calls.append(
            {
                "mode": mode,
                "symbol": symbol,
                "start_time": start_time,
                "end_time": end_time,
                "limit": limit,
                "page": page,
            }
        )
        matching = [row for row in self.rows if start_time <= int(row["updateTime"]) <= end_time]
        offset = int(page or 0) * limit
        return matching[offset : offset + limit]


class RateLimitedGateway(HistoryGateway):
    def trade_rows(self, mode, symbol, *, start_time, end_time, limit, page=None):
        raise RuntimeError('weex {"code":-1003,"msg":"Too much request weight used"}')


def service(path: Path, gateway: HistoryGateway):
    ledger = SQLiteTradeVolumeLedger(path)
    return ledger, DemoTradeVolumeSyncService(gateway, ledger, account_fingerprint("account-a"))


def test_initial_sync_materializes_exact_volume_and_directions(tmp_path: Path) -> None:
    gateway = HistoryGateway(
        [
            order("1", timestamp=1000, quote="700.12345678"),
            order("2", timestamp=2000, quote="699.87654322", side="SELL", position_side="LONG"),
        ]
    )
    ledger, syncer = service(tmp_path / "volume.sqlite3", gateway)
    try:
        result = syncer.sync(start_time=0, end_time=3000)
    finally:
        ledger.close()

    assert result["status"] == "completed"
    assert result["network_requests"] == 1
    assert result["inserted_trades"] == 2
    assert result["summary"]["total_quote_volume"] == "1400"
    assert result["summary"]["opening_quote_volume"] == "700.12345678"
    assert result["summary"]["closing_quote_volume"] == "699.87654322"
    assert result["summary"]["maker_quote_volume"] == "1400"


def test_second_sync_reads_only_overlap_and_deduplicates(tmp_path: Path) -> None:
    gateway = HistoryGateway([order("1", timestamp=1000, quote="700")])
    ledger, syncer = service(tmp_path / "volume.sqlite3", gateway)
    try:
        first = syncer.sync(start_time=0, end_time=2000)
        gateway.rows.append(order("2", timestamp=2500, quote="710"))
        second = syncer.sync(start_time=0, end_time=3000, overlap_ms=1500)
    finally:
        ledger.close()

    assert first["summary"]["total_quote_volume"] == "700"
    assert second["network_requests"] == 1
    assert gateway.calls[-1]["start_time"] == 500
    assert second["inserted_trades"] == 1
    assert second["unchanged_trades"] == 1
    assert second["summary"]["total_quote_volume"] == "1410"


def test_changed_order_replaces_prior_volume_without_double_counting(tmp_path: Path) -> None:
    gateway = HistoryGateway([order("1", timestamp=1000, quote="700")])
    ledger, syncer = service(tmp_path / "volume.sqlite3", gateway)
    try:
        syncer.sync(start_time=0, end_time=2000)
        gateway.rows[0] = order("1", timestamp=1500, quote="725")
        result = syncer.sync(start_time=0, end_time=3000, overlap_ms=2500)
    finally:
        ledger.close()

    assert result["updated_trades"] == 1
    assert result["summary"]["trade_count"] == 1
    assert result["summary"]["total_quote_volume"] == "725"


def test_request_budget_checkpoints_large_backfill(tmp_path: Path) -> None:
    rows = [order(str(index), timestamp=1000 + index, quote="1") for index in range(DEMO_PAGE_LIMIT + 1)]
    gateway = HistoryGateway(rows)
    ledger, syncer = service(tmp_path / "volume.sqlite3", gateway)
    try:
        partial = syncer.sync(start_time=0, end_time=3000, max_requests=1)
        completed = syncer.sync(start_time=0, end_time=3000, max_requests=3)
    finally:
        ledger.close()

    assert partial["status"] == "partial"
    assert partial["history_complete"] is False
    assert partial["summary"]["trade_count"] == 0
    assert gateway.calls[0]["page"] == 0
    assert gateway.calls[1]["page"] == 0
    assert gateway.calls[1]["end_time"] < gateway.calls[0]["end_time"]
    assert completed["status"] == "completed"
    assert completed["summary"]["trade_count"] == DEMO_PAGE_LIMIT + 1


def test_symbol_scopes_are_stored_independently(tmp_path: Path) -> None:
    gateway = HistoryGateway([order("1", timestamp=1000, quote="700")])
    ledger, syncer = service(tmp_path / "volume.sqlite3", gateway)
    try:
        result = syncer.sync(start_time=0, end_time=2000, symbol="BTC")
    finally:
        ledger.close()

    assert gateway.calls[0]["symbol"] == "BTC"
    assert result["summary"]["total_quote_volume"] == "700"


def test_rate_limit_returns_checkpointed_partial_result(tmp_path: Path) -> None:
    gateway = RateLimitedGateway([])
    ledger, syncer = service(tmp_path / "volume.sqlite3", gateway)
    try:
        result = syncer.sync(start_time=0, end_time=2000)
    finally:
        ledger.close()

    assert result["status"] == "rate_limited"
    assert result["history_complete"] is False
    assert result["retry_after_seconds"] == 10
    assert result["summary"]["total_quote_volume"] == "0"


def test_saturated_incremental_window_is_split_without_page_numbers(tmp_path: Path) -> None:
    gateway = HistoryGateway([order("initial", timestamp=1000, quote="1")])
    ledger, syncer = service(tmp_path / "volume.sqlite3", gateway)
    try:
        syncer.sync(start_time=0, end_time=2000)
        gateway.rows.extend(
            order(str(index), timestamp=2200 + index * 2, quote="1") for index in range(DEMO_PAGE_LIMIT + 1)
        )
        result = syncer.sync(start_time=0, end_time=5000, overlap_ms=1, max_requests=10)
    finally:
        ledger.close()

    incremental_calls = gateway.calls[1:]
    assert result["status"] == "completed"
    assert result["summary"]["trade_count"] == DEMO_PAGE_LIMIT + 2
    assert all(call["page"] == 0 for call in incremental_calls)
    assert len(incremental_calls) == 3
