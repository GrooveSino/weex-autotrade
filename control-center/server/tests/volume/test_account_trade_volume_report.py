from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from fleet_api.accounts.repository import InMemoryAccountRepository
from fleet_api.auth.vault import CredentialMaterial, EphemeralCredentialVault
from fleet_api.bootstrap.main_context import FleetAppContext
from fleet_api.models import AccountInstance, InstanceStatus, ProxySnapshot, ProxyType, TradingMode
from fleet_api.services.control.service import FleetControlService
from fleet_api.transport.routes.main_routes_volume import register_trade_volume_report_routes
from fleet_api.volume.core.volume_history import (
    InMemoryTradeVolumeLedger,
    NormalizedTradeFill,
    SQLiteTradeVolumeLedger,
    TradeVolumeAggregate,
)
from fleet_api.volume.reports import AccountTradeVolumeReportError, AccountTradeVolumeReportService

DAY_MS = 24 * 60 * 60 * 1_000
NOW_MS = 2_000_000_000_000


def account(mode: TradingMode = TradingMode.LIVE) -> AccountInstance:
    return AccountInstance(
        id="ins-volume",
        name="Volume account",
        account_tag="report",
        api_key_tail="ABCD",
        mode=mode,
        status=InstanceStatus.STOPPED,
        phase="已停止",
        proxy=ProxySnapshot(type=ProxyType.HTTPS, host="proxy.example:8080"),
    )


def material() -> CredentialMaterial:
    return CredentialMaterial(
        api_key=SecretStr("key"),
        api_secret=SecretStr("secret"),
        passphrase=SecretStr("passphrase"),
        proxy_url=SecretStr("http://user:pass@proxy.example:8080"),
    )


def fill(identity: str, age_days: int, quote: str, *, maker: bool | None = True) -> NormalizedTradeFill:
    return NormalizedTradeFill(
        identity=identity,
        executed_at_ms=NOW_MS - age_days * DAY_MS,
        quote_volume=Decimal(quote),
        symbol="BTCUSDT" if maker is not False else "ETHUSDT",
        maker=maker,
    )


class AuthoritativeReader:
    def __init__(self, fills: tuple[NormalizedTradeFill, ...], *, complete: bool = True) -> None:
        self.fills = fills
        self.complete = complete
        self.calls: list[tuple[str, int, int]] = []
        self.active = 0
        self.peak = 0

    async def __call__(self, account_id: str, start_ms: int, end_ms: int):
        self.calls.append((account_id, start_ms, end_ms))
        self.active += 1
        self.peak = max(self.peak, self.active)
        await asyncio.sleep(0.005)
        try:
            selected = tuple(item for item in self.fills if start_ms <= item.executed_at_ms <= end_ms)
            reason = "history_exhausted" if self.complete else "page_budget_exhausted"
            return selected, self.complete, reason
        finally:
            self.active -= 1


def report_service(
    reader: AuthoritativeReader,
    ledger: InMemoryTradeVolumeLedger,
    projections: list[TradeVolumeAggregate] | None = None,
) -> AccountTradeVolumeReportService:
    return AccountTradeVolumeReportService(
        reader,
        ledger,
        lambda _account_id, aggregate: projections.append(aggregate) if projections is not None else None,
        clock_ms=lambda: NOW_MS,
    )


def test_largest_window_is_scanned_once_and_periods_use_actual_quote_qty() -> None:
    async def scenario() -> None:
        ledger = InMemoryTradeVolumeLedger()
        projections: list[TradeVolumeAggregate] = []
        reader = AuthoritativeReader(
            (
                fill("recent-maker", 1, "50.125", maker=True),
                fill("older-taker", 10, "25.25", maker=False),
                fill("outside", 40, "500"),
            )
        )
        response = await report_service(reader, ledger, projections).report(account(), material(), [30, 7, 30])

        assert reader.calls == [("ins-volume", NOW_MS - 30 * DAY_MS, NOW_MS)]
        assert [period.lookback_days for period in response.periods] == [7, 30]
        assert response.periods[0].total_quote_volume == Decimal("50.125")
        assert response.periods[1].total_quote_volume == Decimal("75.375")
        assert response.periods[1].maker_quote_volume == Decimal("50.125")
        assert response.periods[1].taker_quote_volume == Decimal("25.25")
        assert response.ledger_scanned_fill_count == 2
        assert response.ledger_inserted_fill_count == 2
        assert response.ledger_deduplicated_fill_count == 0
        assert response.ledger_lifetime_quote_volume == Decimal("75.375")
        assert response.ledger_source_complete is False
        assert projections[-1].lifetime == Decimal("75.375")
        assert "order_id" not in str(response.model_dump())

    asyncio.run(scenario())


def test_seven_then_thirty_days_only_adds_older_fills_and_repeats_are_deduplicated() -> None:
    async def scenario() -> None:
        ledger = InMemoryTradeVolumeLedger()
        reader = AuthoritativeReader((fill("recent", 2, "20"), fill("older", 20, "30")))
        service = report_service(reader, ledger)

        week = await service.report(account(), material(), [7])
        month = await service.report(account(), material(), [30])
        repeated = await service.report(account(), material(), [30])

        assert week.ledger_inserted_fill_count == 1
        assert month.ledger_inserted_fill_count == 1
        assert month.ledger_scanned_fill_count == 2
        assert month.ledger_deduplicated_fill_count == 1
        assert month.ledger_lifetime_quote_volume == Decimal("50")
        assert repeated.ledger_inserted_fill_count == 0
        assert repeated.ledger_deduplicated_fill_count == 2
        assert repeated.ledger_lifetime_quote_volume == Decimal("50")
        assert ledger.aggregate("ins-volume", 0).fill_count == 2

    asyncio.run(scenario())


def test_sqlite_ledger_deduplicates_overlapping_manual_reports(tmp_path) -> None:
    async def scenario() -> None:
        ledger = SQLiteTradeVolumeLedger(tmp_path / "fleet.sqlite3")
        try:
            reader = AuthoritativeReader((fill("recent", 2, "20"), fill("older", 20, "30")))
            service = AccountTradeVolumeReportService(
                reader,
                ledger,
                lambda _account_id, _aggregate: None,
                clock_ms=lambda: NOW_MS,
            )

            week = await service.report(account(), material(), [7])
            month = await service.report(account(), material(), [30])
            repeated = await service.report(account(), material(), [30])

            assert [week.ledger_inserted_fill_count, month.ledger_inserted_fill_count] == [1, 1]
            assert repeated.ledger_inserted_fill_count == 0
            assert repeated.ledger_lifetime_quote_volume == Decimal("50")
            assert ledger.aggregate("ins-volume", 0).fill_count == 2
        finally:
            ledger.close()

    asyncio.run(scenario())


def test_manual_report_reuses_background_synced_fill_without_double_counting() -> None:
    async def scenario() -> None:
        ledger = InMemoryTradeVolumeLedger()
        already_synced = fill("shared-history-fill", 2, "21.75")
        assert ledger.record_account_fills("ins-volume", "live", (already_synced,)) == 1
        projections: list[TradeVolumeAggregate] = []
        report = report_service(AuthoritativeReader((already_synced,)), ledger, projections)

        response = await report.report(account(), material(), [7])

        assert response.ledger_inserted_fill_count == 0
        assert response.ledger_deduplicated_fill_count == 1
        assert response.ledger_lifetime_quote_volume == Decimal("21.75")
        assert ledger.aggregate("ins-volume", 0).fill_count == 1
        assert projections[-1].lifetime == Decimal("21.75")

    asyncio.run(scenario())


def test_conflicting_identity_fails_without_changing_existing_total() -> None:
    async def scenario() -> None:
        ledger = InMemoryTradeVolumeLedger()
        reader = AuthoritativeReader((fill("same", 1, "10"),))
        service = report_service(reader, ledger)
        await service.report(account(), material(), [7])
        reader.fills = (fill("same", 1, "11"),)

        with pytest.raises(AccountTradeVolumeReportError, match="没有重复累计") as raised:
            await service.report(account(), material(), [7])
        assert raised.value.code == "trade_history_conflict"
        assert ledger.aggregate("ins-volume", 0).lifetime == Decimal("10")

    asyncio.run(scenario())


def test_untrusted_or_zero_quote_fills_never_enter_ledger_and_incomplete_scan_stays_incomplete() -> None:
    async def scenario() -> None:
        ledger = InMemoryTradeVolumeLedger()
        reader = AuthoritativeReader(
            (
                fill("valid", 1, "12.5"),
                fill("zero", 1, "0"),
                NormalizedTradeFill(
                    identity="untrusted",
                    executed_at_ms=NOW_MS - DAY_MS,
                    quote_volume=Decimal("99"),
                    symbol="BTCUSDT",
                    authoritative=False,
                ),
            ),
            complete=False,
        )
        response = await report_service(reader, ledger).report(account(), material(), [7])

        assert response.ledger_inserted_fill_count == 1
        assert response.ledger_lifetime_quote_volume == Decimal("12.5")
        assert response.ledger_source_complete is False
        assert response.periods[0].complete is False
        assert response.periods[0].warnings

    asyncio.run(scenario())


def test_report_rejects_demo_and_missing_credentials_with_actionable_chinese_errors() -> None:
    async def scenario() -> None:
        service = report_service(AuthoritativeReader(()), InMemoryTradeVolumeLedger())
        for instance, credentials, expected_code in (
            (account(TradingMode.DEMO), material(), "live_account_required"),
            (account(), None, "credentials_missing"),
        ):
            with pytest.raises(AccountTradeVolumeReportError) as raised:
                await service.report(instance, credentials, [7])
            assert raised.value.code == expected_code
            assert "。" in raised.value.message
            assert raised.value.action

    asyncio.run(scenario())


def test_same_account_report_calls_are_serialized() -> None:
    async def scenario() -> None:
        reader = AuthoritativeReader((fill("one", 1, "1"),))
        service = report_service(reader, InMemoryTradeVolumeLedger())
        await asyncio.gather(
            service.report(account(), material(), [7]),
            service.report(account(), material(), [7]),
        )
        assert reader.peak == 1

    asyncio.run(scenario())


def test_volume_projection_changes_only_account_totals() -> None:
    repository = InMemoryAccountRepository()
    stored = repository.create(account())
    service = FleetControlService(repository, EphemeralCredentialVault())
    updated = service.apply_volume_aggregate(
        stored.id,
        TradeVolumeAggregate(Decimal("123.5"), Decimal("20.25"), 3, False),
    )

    assert updated.volume.lifetime == 123.5
    assert updated.volume.today == 20.25
    assert updated.volume.complete is False
    assert updated.status == stored.status
    assert updated.phase == stored.phase
    assert updated.wallet == stored.wallet
    assert updated.exposure == stored.exposure
    assert updated.strategy_progress == stored.strategy_progress


def test_manual_report_projects_totals_without_changing_strategy_progress() -> None:
    async def scenario() -> None:
        repository = InMemoryAccountRepository()
        stored = repository.create(account())
        control = FleetControlService(repository, EphemeralCredentialVault())
        report = AccountTradeVolumeReportService(
            AuthoritativeReader((fill("manual-history", 0, "88.5"),)),
            InMemoryTradeVolumeLedger(),
            control.apply_volume_aggregate,
            clock_ms=lambda: NOW_MS,
        )

        await report.report(stored, material(), [7])
        updated = control.get_instance(stored.id)

        assert updated.volume.lifetime == 88.5
        assert updated.volume.today == 88.5
        assert updated.strategy_progress == stored.strategy_progress
        assert updated.execution_lifecycle == stored.execution_lifecycle

    asyncio.run(scenario())


def test_post_route_imports_history_publishes_snapshot_and_returns_ledger_summary() -> None:
    reader = AuthoritativeReader((fill("route", 1, "18.75"),))
    report = report_service(reader, InMemoryTradeVolumeLedger())
    published = 0

    async def publish_snapshot() -> None:
        nonlocal published
        published += 1

    stored = account()
    ctx = FleetAppContext(
        selected=SimpleNamespace(adapter="weex-readonly"),
        service=SimpleNamespace(get_instance=lambda _instance_id: stored),
        vault=SimpleNamespace(get=lambda _instance_id: material()),
        account_trade_volume_report_service=report,
        publish_snapshot=publish_snapshot,
    )
    app = FastAPI()
    register_trade_volume_report_routes(app, ctx)

    with TestClient(app) as api:
        response = api.post("/api/v1/instances/ins-volume/trade-volume-report?lookback_days=7")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["ledgerInsertedFillCount"] == 1
        assert response.json()["ledgerLifetimeQuoteVolume"] == "18.75"
        assert response.json()["accountVolume"]["lifetimeQuoteVolume"] == "18.75"
        response = api.get("/api/v1/instances/ins-volume/trade-volume-report?lookback_days=7")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["ledgerInsertedFillCount"] == 0
    assert published == 2
