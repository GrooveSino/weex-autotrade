import asyncio
from decimal import Decimal
from pathlib import Path

import pytest

from fleet_api.models import (
    AccountInstance,
    InstanceStatus,
    ProxySnapshot,
    ProxyType,
    TradingMode,
)
from fleet_api.repository import SQLiteAccountRepository
from fleet_api.volume_history import (
    InMemoryTradeVolumeLedger,
    SessionVolumeService,
    SQLiteTradeVolumeLedger,
    TradeHistoryContext,
    TradeHistoryPage,
    TradeHistorySynchronizer,
)

from .test_volume_sessions_support import (
    fill,
)


def test_session_window_can_complete_while_lifetime_history_remains_incomplete() -> None:
    class SessionWindowSource:
        async def fetch_page(self, context, *, cursor, limit):
            return TradeHistoryPage(
                fills=(fill("window", "10", "close"),),
                next_cursor=None,
                complete=False,
                window_complete=True,
                high_watermark_ms=1_000,
            )

    async def scenario() -> None:
        ledger = InMemoryTradeVolumeLedger()
        service = SessionVolumeService(ledger)
        instance = AccountInstance(
            id="window-account",
            name="window",
            account_tag="test",
            api_key_tail="ABCD",
            mode=TradingMode.LIVE,
            status=InstanceStatus.RUNNING,
            phase="running",
            proxy=ProxySnapshot(type=ProxyType.HTTPS, host="example:443"),
        )
        service.start(
            session_id="window-session",
            account_id=instance.id,
            mode="live",
            started_at_ms=500,
            target_quote_volume=Decimal("10"),
        )
        await TradeHistorySynchronizer(ledger).sync(
            instance.id,
            TradeHistoryContext(instance, None),
            SessionWindowSource(),
            today_start_ms=0,
            coverage_start_ms=500,
        )
        projection = service.progress("window-session")
        assert ledger.aggregate(instance.id, 0).complete is False
        assert projection["source_complete"] is True
        assert projection["status"] == "active"
        assert projection["verified_quote_volume"] == "10"

    asyncio.run(scenario())

def test_sqlite_strategy_run_history_uses_stable_cursor_pagination(tmp_path: Path) -> None:
    path = tmp_path / "history.db"
    repository = SQLiteAccountRepository(path)
    repository.create(
        AccountInstance(
            id="history-account",
            name="history",
            account_tag="test",
            api_key_tail="ABCD",
            mode=TradingMode.LIVE,
            status=InstanceStatus.STOPPED,
            phase="idle",
            proxy=ProxySnapshot(type=ProxyType.NONE, host="none"),
        )
    )
    ledger = SQLiteTradeVolumeLedger(path)
    service = SessionVolumeService(ledger)
    for index in range(3):
        session_id = f"run-{index}"
        service.start(
            session_id=session_id,
            account_id="history-account",
            mode="live",
            started_at_ms=1_000 + index,
            target_quote_volume=Decimal("5"),
        )
        ledger.update_session(session_id, source_complete=True, stale=False, pending_sync=False)
        service.finalize(
            session_id,
            result="stopped",
            reason="test",
            finished_at_ms=2_000 + index,
            final_lifetime_quote_volume=Decimal(index),
        )

    first, cursor = ledger.list_sessions("history-account", "live", limit=2)
    second, final_cursor = ledger.list_sessions("history-account", "live", limit=2, cursor=cursor)

    assert [row["session_id"] for row in first] == ["run-2", "run-1"]
    assert cursor == "run-1"
    assert [row["session_id"] for row in second] == ["run-0"]
    assert final_cursor is None
    ledger.close()
    repository.close()

@pytest.mark.parametrize("ledger_kind", ["memory", "sqlite"])
def test_terminal_session_keeps_terminal_state_and_marks_audit_discrepant_on_fill_conflict(
    tmp_path: Path, ledger_kind: str
) -> None:
    repository = None
    if ledger_kind == "sqlite":
        path = tmp_path / "conflict.db"
        repository = SQLiteAccountRepository(path)
        repository.create(
            AccountInstance(
                id="conflict-account",
                name="conflict",
                account_tag="test",
                api_key_tail="ABCD",
                mode=TradingMode.LIVE,
                status=InstanceStatus.STOPPED,
                phase="idle",
                proxy=ProxySnapshot(type=ProxyType.NONE, host="none"),
            )
        )
        ledger = SQLiteTradeVolumeLedger(path)
    else:
        ledger = InMemoryTradeVolumeLedger()
    service = SessionVolumeService(ledger)
    service.start(
        session_id="conflict-run",
        account_id="conflict-account",
        mode="live",
        started_at_ms=500,
        target_quote_volume=Decimal("5"),
    )
    ledger.update_session("conflict-run", source_complete=True, stale=False, pending_sync=False)
    service.finalize(
        "conflict-run",
        result="stopped",
        reason="manual_stop",
        finished_at_ms=2_000,
        final_lifetime_quote_volume=Decimal("5"),
    )

    ledger.mark_sessions_reconciliation("conflict-account", "live", discrepancy=Decimal("1"))
    projection = service.progress("conflict-run")

    assert projection["status"] == "stopped"
    assert projection["audit_status"] == "discrepant"
    assert projection["result"] == "stopped"
    assert projection["reconciliation_required"] is True
    assert ledger.active_session("conflict-account", "live") is None
    ledger.close()
    if repository is not None:
        repository.close()
