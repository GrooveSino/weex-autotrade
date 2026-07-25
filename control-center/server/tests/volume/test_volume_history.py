import asyncio
from decimal import Decimal
from pathlib import Path

import pytest

from fleet_api.accounts.repository import SQLiteAccountRepository
from fleet_api.models import AccountInstance, InstanceStatus, ProxySnapshot, ProxyType, TradingMode
from fleet_api.volume.core.volume_history import (
    FillConflictError,
    InMemoryTradeVolumeLedger,
    NormalizedTradeFill,
    SQLiteTradeVolumeLedger,
    TradeHistoryContext,
    TradeHistoryPage,
    TradeHistorySynchronizer,
    shanghai_day_start_ms,
    utc_day_start_ms,
)

TODAY_START = 1784332800000


def account(instance_id: str = "ins-volume") -> AccountInstance:
    return AccountInstance(
        id=instance_id,
        name="Volume account",
        account_tag="history",
        api_key_tail="ABCD",
        mode=TradingMode.DEMO,
        status=InstanceStatus.STOPPED,
        phase="等待同步",
        proxy=ProxySnapshot(type=ProxyType.HTTPS, host="proxy.example.com:9000"),
    )


def fill(identity: str, amount: str, timestamp: int = TODAY_START) -> NormalizedTradeFill:
    return NormalizedTradeFill(
        identity=identity,
        executed_at_ms=timestamp,
        quote_volume=Decimal(amount),
        symbol="BTCUSDT",
    )


class PageSource:
    def __init__(self, pages: dict[str | None, TradeHistoryPage]) -> None:
        self.pages = pages
        self.calls: list[tuple[str | None, int]] = []

    async def fetch_page(
        self,
        context: TradeHistoryContext,
        *,
        cursor: str | None,
        limit: int,
    ) -> TradeHistoryPage:
        assert context.instance.id == "ins-volume"
        self.calls.append((cursor, limit))
        return self.pages[cursor]


def test_full_history_sync_deduplicates_overlapping_pages_and_is_idempotent() -> None:
    async def scenario() -> None:
        ledger = InMemoryTradeVolumeLedger()
        source = PageSource(
            {
                None: TradeHistoryPage((fill("trade-1", "10"), fill("trade-2", "20")), "next"),
                "next": TradeHistoryPage((fill("trade-2", "20"), fill("trade-3", "30")), None),
            }
        )
        synchronizer = TradeHistorySynchronizer(ledger, page_size=2)
        context = TradeHistoryContext(account(), None)

        first = await synchronizer.sync(
            "ins-volume",
            context,
            source,
            today_start_ms=TODAY_START,
        )
        second = await synchronizer.sync(
            "ins-volume",
            context,
            source,
            today_start_ms=TODAY_START,
        )

        assert first.aggregate.lifetime == Decimal("60")
        assert first.aggregate.today == Decimal("60")
        assert first.aggregate.fill_count == 3
        assert first.aggregate.complete is True
        assert first.pages_fetched == 2
        assert first.fills_inserted == 3
        assert second.fills_inserted == 0
        assert second.aggregate == first.aggregate

    asyncio.run(scenario())


@pytest.mark.parametrize("ledger_factory", [InMemoryTradeVolumeLedger])
def test_conflicting_duplicate_rolls_back_entire_batch(ledger_factory) -> None:
    ledger = ledger_factory()
    ledger.record("ins-volume", (fill("stable", "10"),))

    with pytest.raises(FillConflictError, match="changed across history pages"):
        ledger.record("ins-volume", (fill("new", "5"), fill("stable", "11")))

    aggregate = ledger.aggregate("ins-volume", TODAY_START)
    assert aggregate.lifetime == Decimal("10")
    assert aggregate.fill_count == 1


def test_cursor_loop_and_page_budget_never_claim_complete_history() -> None:
    async def scenario() -> None:
        context = TradeHistoryContext(account(), None)
        loop_ledger = InMemoryTradeVolumeLedger()
        loop_source = PageSource(
            {
                None: TradeHistoryPage((fill("one", "1"),), "repeat"),
                "repeat": TradeHistoryPage((fill("two", "2"),), "repeat"),
            }
        )
        loop_result = await TradeHistorySynchronizer(loop_ledger).sync(
            "ins-volume", context, loop_source, today_start_ms=TODAY_START
        )

        budget_ledger = InMemoryTradeVolumeLedger()
        budget_source = PageSource(
            {
                None: TradeHistoryPage((fill("one", "1"),), "more"),
                "more": TradeHistoryPage((fill("two", "2"),), None),
            }
        )
        budget_result = await TradeHistorySynchronizer(budget_ledger, max_pages=1).sync(
            "ins-volume", context, budget_source, today_start_ms=TODAY_START
        )
        resumed_result = await TradeHistorySynchronizer(budget_ledger, max_pages=1).sync(
            "ins-volume",
            context,
            budget_source,
            today_start_ms=TODAY_START,
            cursor=budget_result.next_cursor,
        )

        incomplete_ledger = InMemoryTradeVolumeLedger()
        incomplete_result = await TradeHistorySynchronizer(incomplete_ledger).sync(
            "ins-volume",
            context,
            PageSource({None: TradeHistoryPage((fill("partial", "9"),), None, complete=False)}),
            today_start_ms=TODAY_START,
        )

        assert loop_result.stop_reason == "cursor_loop"
        assert loop_result.aggregate.complete is False
        assert budget_result.stop_reason == "page_budget_exhausted"
        assert budget_result.aggregate.complete is False
        assert budget_result.next_cursor == "more"
        assert resumed_result.stop_reason == "history_exhausted"
        assert resumed_result.aggregate.lifetime == Decimal("3")
        assert resumed_result.aggregate.complete is True
        assert incomplete_result.stop_reason == "source_incomplete"
        assert incomplete_result.aggregate.lifetime == Decimal("9")
        assert incomplete_result.aggregate.complete is False

    asyncio.run(scenario())


def test_sqlite_ledger_persists_exact_decimal_totals_and_cascades_with_account(tmp_path: Path) -> None:
    path = tmp_path / "fleet.db"
    repository = SQLiteAccountRepository(path)
    repository.create(account())
    ledger = SQLiteTradeVolumeLedger(path)
    old_fill = fill("old", "0.123456789123456789", TODAY_START - 1)
    today_fill = fill("today", "999999999.876543210876543211", TODAY_START)
    ledger.record("ins-volume", (old_fill, today_fill))
    ledger.set_complete("ins-volume", True)
    ledger.close()

    restored = SQLiteTradeVolumeLedger(path)
    aggregate = restored.aggregate("ins-volume", TODAY_START)
    assert aggregate.lifetime == Decimal("1000000000.000000000000000000")
    assert aggregate.today == Decimal("999999999.876543210876543211")
    assert aggregate.fill_count == 2
    assert aggregate.complete is True

    repository.delete("ins-volume")
    assert restored.aggregate("ins-volume", TODAY_START).fill_count == 0
    restored.close()
    repository.close()


def test_sqlite_conflict_rolls_back_entire_batch(tmp_path: Path) -> None:
    path = tmp_path / "fleet.db"
    repository = SQLiteAccountRepository(path)
    repository.create(account())
    ledger = SQLiteTradeVolumeLedger(path)
    ledger.record("ins-volume", (fill("stable", "10"),))

    with pytest.raises(FillConflictError):
        ledger.record("ins-volume", (fill("new", "5"), fill("stable", "11")))

    aggregate = ledger.aggregate("ins-volume", TODAY_START)
    assert aggregate.lifetime == Decimal("10")
    assert aggregate.fill_count == 1
    ledger.close()
    repository.close()


def test_sqlite_checkpoint_preserves_split_window_state_across_restart(tmp_path: Path) -> None:
    path = tmp_path / "fleet.db"
    repository = SQLiteAccountRepository(path)
    repository.create(account())
    ledger = SQLiteTradeVolumeLedger(path)
    state = {
        "pending_windows": [[1_000, 1_999], [0, 999]],
        "expected_cursor": "scan-7-2",
        "scan_id": 7,
        "page_sequence": 2,
        "coverage_complete": False,
        "truncated": False,
        "active": True,
        "scan_start_ms": 0,
        "scan_end_ms": 1_999,
    }
    ledger.save_sync_checkpoint(
        "ins-volume",
        "demo",
        cursor="scan-7-2",
        high_watermark_ms=999,
        pending=True,
        source_complete=False,
        coverage_complete=False,
        stale=False,
        scan_state=state,
        sync_reason="initial_baseline",
        next_sync_at_ms=123,
        last_success_at_ms=99,
        initial_baseline_state="running",
    )
    ledger.close()

    restored = SQLiteTradeVolumeLedger(path)
    checkpoint = restored.sync_checkpoint("ins-volume", "demo")
    assert checkpoint is not None
    assert checkpoint["cursor"] == "scan-7-2"
    assert checkpoint["scan_state"] == state
    assert checkpoint["last_success_at_ms"] == 99
    assert checkpoint["initial_baseline_state"] == "running"
    restored.close()
    repository.close()


def test_utc_day_start_is_stable_across_timezones() -> None:
    assert utc_day_start_ms(1784347199999) == TODAY_START
    assert utc_day_start_ms(1784347200000) == TODAY_START


def test_shanghai_day_start_uses_beijing_natural_day() -> None:
    shanghai_start = 1784304000000

    assert shanghai_day_start_ms(shanghai_start) == shanghai_start
    assert shanghai_day_start_ms(shanghai_start + 86_399_999) == shanghai_start
