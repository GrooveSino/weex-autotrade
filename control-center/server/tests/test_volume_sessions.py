import asyncio
from decimal import Decimal
from pathlib import Path

import pytest

from fleet_api.models import AccountInstance, InstanceStatus, ProxySnapshot, ProxyType, TradingMode
from fleet_api.repository import SQLiteAccountRepository
from fleet_api.volume_history import (
    InMemoryTradeVolumeLedger,
    NormalizedTradeFill,
    SessionVolumeService,
    SQLiteTradeVolumeLedger,
    TradeHistoryContext,
    TradeHistorySynchronizer,
)


def fill(fill_id: str, quote: str, action: str, *, authoritative: bool = True, maker: bool | None = True):
    return NormalizedTradeFill(
        identity=fill_id,
        executed_at_ms=1_000,
        quote_volume=Decimal(quote),
        symbol="BTCUSDT",
        order_id=f"order-{fill_id}",
        base_quantity=Decimal("0.001"),
        position_action=action,
        maker=maker,
        authoritative=authoritative,
    )


def test_session_counts_open_and_close_once_and_ignores_planned_amounts() -> None:
    ledger = InMemoryTradeVolumeLedger()
    service = SessionVolumeService(ledger)
    service.start(session_id="s1", account_id="a1", mode="live", started_at_ms=500, target_quote_volume=Decimal("20"))
    ledger.record_account_fills("a1", "live", (fill("open", "7", "open"), fill("close", "8", "close")))
    ledger.update_session("s1", source_complete=True, stale=False, pending_sync=False)

    projection = service.progress("s1")
    assert projection["verified_quote_volume"] == "15"
    assert projection["remaining_quote_volume"] == "5"
    assert projection["opening_quote_volume"] == "7"
    assert projection["closing_quote_volume"] == "8"

    ledger.record_account_fills("a1", "live", (fill("open", "7", "open"),))
    assert service.progress("s1")["verified_quote_volume"] == "15"


def test_incomplete_source_cannot_complete_target() -> None:
    ledger = InMemoryTradeVolumeLedger()
    service = SessionVolumeService(ledger)
    service.start(session_id="s2", account_id="a2", mode="demo", started_at_ms=500, target_quote_volume=Decimal("10"))
    ledger.record_account_fills("a2", "demo", (fill("partial", "10", "open"),))
    ledger.update_session("s2", source_complete=False, stale=True, pending_sync=True)

    projection = service.progress("s2")
    assert projection["verified_quote_volume"] == "0"
    assert projection["remaining_quote_volume"] == "10"
    assert projection["status"] == "stale"
    assert projection["retry_allowed"] is False


def test_reconcile_marks_missing_fill_as_required() -> None:
    ledger = InMemoryTradeVolumeLedger()
    service = SessionVolumeService(ledger)
    service.start(session_id="s3", account_id="a3", mode="live", started_at_ms=500, target_quote_volume=Decimal("10"))
    ledger.record_account_fills("a3", "live", (fill("local", "4", "open"),))
    projection = service.reconcile("s3", (fill("remote", "10", "open"),), reconciled_at_ms=2_000)
    assert projection["reconciliation_required"] is True
    assert projection["stale"] is True


def test_sqlite_session_and_checkpoint_survive_restart(tmp_path: Path) -> None:
    path = tmp_path / "fleet.db"
    repository = SQLiteAccountRepository(path)
    repository.create(
        AccountInstance(
            id="a4",
            name="persisted",
            account_tag="test",
            api_key_tail="ABCD",
            mode=TradingMode.LIVE,
            status=InstanceStatus.STOPPED,
            phase="idle",
            proxy=ProxySnapshot(type=ProxyType.HTTPS, host="example:443"),
        )
    )
    ledger = SQLiteTradeVolumeLedger(path)
    service = SessionVolumeService(ledger)
    service.start(session_id="s4", account_id="a4", mode="live", started_at_ms=500, target_quote_volume=Decimal("8"))
    ledger.record_account_fills("a4", "live", (fill("persisted", "8", "close"),))
    ledger.save_sync_checkpoint(
        "a4",
        "live",
        cursor=None,
        high_watermark_ms=1_000,
        pending=False,
        source_complete=True,
        coverage_complete=True,
        stale=False,
    )
    ledger.refresh_sessions("a4", "live", now_ms=1_100, source_complete=True, stale=False)
    ledger.close()

    restored = SQLiteTradeVolumeLedger(path)
    projection = restored.session_projection("s4")
    assert projection["status"] == "completed"
    assert projection["verified_quote_volume"] == "8"
    assert restored.sync_checkpoint("a4", "live")["high_watermark_ms"] == 1_000
    restored.close()
    repository.close()


def test_sync_timeout_marks_previous_session_projection_stale() -> None:
    class TimeoutSource:
        async def fetch_page(self, context, *, cursor, limit):
            raise TimeoutError

    async def scenario() -> None:
        ledger = InMemoryTradeVolumeLedger()
        service = SessionVolumeService(ledger)
        instance = AccountInstance(
            id="a5",
            name="timeout",
            account_tag="test",
            api_key_tail="ABCD",
            mode=TradingMode.LIVE,
            status=InstanceStatus.STOPPED,
            phase="idle",
            proxy=ProxySnapshot(type=ProxyType.HTTPS, host="example:443"),
        )
        service.start(
            session_id="s5",
            account_id="a5",
            mode="live",
            started_at_ms=500,
            target_quote_volume=Decimal("9"),
        )
        ledger.update_session("s5", source_complete=True, stale=False, pending_sync=False)
        with pytest.raises(TimeoutError):
            await TradeHistorySynchronizer(ledger).sync(
                "a5",
                TradeHistoryContext(instance, None),
                TimeoutSource(),
                today_start_ms=0,
            )
        projection = service.progress("s5")
        assert projection["stale"] is True
        assert projection["status"] == "stale"

    asyncio.run(scenario())
