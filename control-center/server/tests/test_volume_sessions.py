import asyncio
from decimal import Decimal
from pathlib import Path

import pytest

from fleet_api.models import (
    AccountInstance,
    InstanceStatus,
    ProxySnapshot,
    ProxyType,
    StrategyTargetMode,
    TradingMode,
)
from fleet_api.repository import SQLiteAccountRepository
from fleet_api.strategy import StrategyTargetReached, resolve_strategy_run_plan
from fleet_api.volume_history import (
    InMemoryTradeVolumeLedger,
    NormalizedTradeFill,
    SessionVolumeService,
    SQLiteTradeVolumeLedger,
    TradeHistoryContext,
    TradeHistoryPage,
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
    assert projection["starting_available_balance_quote"] is None
    assert projection["ending_available_balance_quote"] is None
    assert projection["available_balance_change_quote"] is None

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
    assert projection["status"] == "verification_pending"
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
    service.start(
        session_id="s4",
        account_id="a4",
        mode="live",
        started_at_ms=500,
        target_quote_volume=Decimal("8"),
        strategy_id="persisted-strategy",
        strategy_name="Persisted strategy",
        strategy_version=7,
        target_mode="incremental",
        strategy_target_quote_volume=Decimal("8"),
        baseline_lifetime_quote_volume=Decimal("92"),
        starting_available_balance_quote=Decimal("100.50"),
    )
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
    service.finalize(
        "s4",
        result="completed",
        reason="target_reached",
        finished_at_ms=1_200,
        final_lifetime_quote_volume=Decimal("100"),
        ending_available_balance_quote=Decimal("100.12"),
    )
    ledger.close()

    restored = SQLiteTradeVolumeLedger(path)
    projection = restored.session_projection("s4")
    assert projection["status"] == "completed"
    assert projection["verified_quote_volume"] == "8"
    assert projection["strategy_id"] == "persisted-strategy"
    assert projection["strategy_name"] == "Persisted strategy"
    assert projection["strategy_version"] == 7
    assert projection["baseline_lifetime_quote_volume"] == "92"
    assert projection["finished_at_ms"] == 1_200
    assert projection["result"] == "completed"
    assert projection["final_lifetime_quote_volume"] == "100"
    assert projection["starting_available_balance_quote"] == "100.50"
    assert projection["ending_available_balance_quote"] == "100.12"
    assert projection["available_balance_change_quote"] == "-0.38"
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
        assert projection["status"] == "verification_pending"

    asyncio.run(scenario())


def test_completed_incremental_run_allows_a_fresh_full_target() -> None:
    ledger = InMemoryTradeVolumeLedger()
    service = SessionVolumeService(ledger)
    service.start(
        session_id="run-1",
        account_id="account",
        mode="live",
        started_at_ms=500,
        target_quote_volume=Decimal("10"),
        strategy_id="strategy",
        strategy_name="Incremental",
        strategy_version=3,
        target_mode="incremental",
        strategy_target_quote_volume=Decimal("10"),
    )
    ledger.record_account_fills("account", "live", (fill("done", "10", "close"),))
    ledger.update_session("run-1", source_complete=True, stale=False, pending_sync=False)
    service.finalize(
        "run-1",
        result="completed",
        reason="target_reached",
        finished_at_ms=2_000,
        final_lifetime_quote_volume=Decimal("110"),
    )

    assert ledger.active_session("account", "live") is None
    second = service.start(
        session_id="run-2",
        account_id="account",
        mode="live",
        started_at_ms=3_000,
        target_quote_volume=Decimal("10"),
        target_mode="incremental",
        strategy_target_quote_volume=Decimal("10"),
    )
    assert second["session_id"] == "run-2"
    assert second["target_quote_volume"] == "10"
    assert second["verified_quote_volume"] == "0"


def test_stopped_incremental_run_is_archived_and_next_run_does_not_reuse_remaining() -> None:
    ledger = InMemoryTradeVolumeLedger()
    service = SessionVolumeService(ledger)
    service.start(
        session_id="stopped-1",
        account_id="account",
        mode="live",
        started_at_ms=500,
        target_quote_volume=Decimal("10"),
    )
    ledger.record_account_fills("account", "live", (fill("partial-stop", "4", "open"),))
    ledger.update_session("stopped-1", source_complete=True, stale=False, pending_sync=False)
    stopped = service.finalize(
        "stopped-1",
        result="stopped",
        reason="manual_stop",
        finished_at_ms=2_000,
        final_lifetime_quote_volume=Decimal("104"),
    )

    assert stopped["status"] == "stopped"
    assert stopped["verified_quote_volume"] == "4"
    next_run = service.start(
        session_id="stopped-2",
        account_id="account",
        mode="live",
        started_at_ms=3_000,
        target_quote_volume=Decimal("10"),
    )
    assert next_run["remaining_quote_volume"] == "10"


def test_lifetime_run_plan_uses_authoritative_residual_and_blocks_reached_target() -> None:
    instance = AccountInstance(
        id="lifetime",
        name="lifetime",
        account_tag="test",
        api_key_tail="ABCD",
        mode=TradingMode.LIVE,
        status=InstanceStatus.STOPPED,
        phase="idle",
        proxy=ProxySnapshot(type=ProxyType.HTTPS, host="example:443"),
    )
    instance = instance.model_copy(
        update={
            "strategy": instance.strategy.model_copy(
                update={"target_mode": StrategyTargetMode.LIFETIME, "target_volume_quote": Decimal("200")}
            ),
            "volume": instance.volume.model_copy(update={"lifetime": 150.0, "complete": True}),
        },
        deep=True,
    )
    plan = resolve_strategy_run_plan(instance, None)
    assert plan.execution_target_quote_volume == Decimal("50")
    assert plan.baseline_lifetime_quote_volume == Decimal("150.0")

    reached = instance.model_copy(
        update={"volume": instance.volume.model_copy(update={"lifetime": 200.0})},
        deep=True,
    )
    with pytest.raises(StrategyTargetReached):
        resolve_strategy_run_plan(reached, None)


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
        assert projection["status"] == "completed"

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
def test_terminal_session_returns_to_verification_pending_on_fill_conflict(
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

    assert projection["status"] == "verification_pending"
    assert projection["result"] == "stopped"
    assert projection["reconciliation_required"] is True
    assert ledger.active_session("conflict-account", "live") is not None
    ledger.close()
    if repository is not None:
        repository.close()
