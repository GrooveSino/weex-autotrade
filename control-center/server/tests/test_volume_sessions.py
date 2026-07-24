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
    SessionVolumeService,
    SQLiteTradeVolumeLedger,
    TradeHistoryContext,
    TradeHistorySynchronizer,
)

from .test_volume_sessions_support import (
    fill,
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
    assert projection["status"] == "active"
    assert projection["audit_status"] == "pending"
    assert projection["retry_allowed"] is False


def test_reconcile_marks_missing_fill_as_required() -> None:
    ledger = InMemoryTradeVolumeLedger()
    service = SessionVolumeService(ledger)
    service.start(session_id="s3", account_id="a3", mode="live", started_at_ms=500, target_quote_volume=Decimal("10"))
    ledger.record_account_fills("a3", "live", (fill("local", "4", "open"),))
    projection = service.reconcile("s3", (fill("remote", "10", "open"),), reconciled_at_ms=2_000)
    assert projection["reconciliation_required"] is True
    assert projection["stale"] is True


def test_recover_stopped_preserves_authoritative_volume_and_unblocks_next_run() -> None:
    ledger = InMemoryTradeVolumeLedger()
    service = SessionVolumeService(ledger)
    service.start(
        session_id="recoverable",
        account_id="recover-account",
        mode="live",
        started_at_ms=500,
        target_quote_volume=Decimal("20"),
    )
    service.mark_recovering(
        "recoverable",
        reason="control_plane_restart",
        finished_at_ms=1_500,
    )

    projection = service.recover_stopped(
        "recoverable",
        (fill("recovered-open", "7", "open"), fill("recovered-close", "8", "close")),
        reconciled_at_ms=2_000,
        ending_available_balance_quote=Decimal("99.5"),
    )

    assert projection["status"] == "stopped"
    assert projection["verified_quote_volume"] == "15"
    assert projection["remaining_quote_volume"] == "5"
    assert projection["reconciliation_required"] is False
    assert projection["uncertain_order_state"] is False
    assert projection["result_reason"] == "automatic_startup_recovery"
    assert ledger.active_session("recover-account", "live") is None


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
        assert projection["status"] == "active"
        assert projection["audit_status"] == "pending"

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
                update={
                    "target_mode": StrategyTargetMode.LIFETIME,
                    "target_volume_quote": Decimal("200"),
                    "target_volume_quote_min": Decimal("200"),
                    "target_volume_quote_max": Decimal("200"),
                }
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
