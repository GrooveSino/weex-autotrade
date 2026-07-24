from decimal import Decimal

from fleet_api.volume_contracts import NormalizedTradeFill
from fleet_api.volume_history import (
    InMemoryTradeVolumeLedger,
    SessionVolumeService,
    SQLiteTradeVolumeLedger,
)


def test_terminal_session_does_not_absorb_fills_from_a_later_run() -> None:
    ledger = InMemoryTradeVolumeLedger()
    service = SessionVolumeService(ledger)
    service.start(
        session_id="first",
        account_id="a3",
        mode="live",
        started_at_ms=1_000,
        target_quote_volume=Decimal("10"),
    )
    ledger.update_session("first", finished_at_ms=2_000)
    first_fill = NormalizedTradeFill(
        identity="first-fill",
        executed_at_ms=1_500,
        quote_volume=Decimal("4"),
        symbol="BTCUSDT",
        position_action="open",
    )
    later_fill = NormalizedTradeFill(
        identity="later-fill",
        executed_at_ms=3_000,
        quote_volume=Decimal("6"),
        symbol="BTCUSDT",
        position_action="open",
    )
    ledger.record_account_fills("a3", "live", (first_fill, later_fill))

    projection = service.reconcile("first", (first_fill,), reconciled_at_ms=2_500)

    assert projection["reconciliation_required"] is False
    assert projection["verified_quote_volume"] == "4"
    assert projection["fill_count"] == 1


def test_sqlite_terminal_session_does_not_absorb_fills_from_a_later_run(tmp_path) -> None:
    ledger = SQLiteTradeVolumeLedger(tmp_path / "volume.sqlite")
    service = SessionVolumeService(ledger)
    service.start(
        session_id="first",
        account_id="a3",
        mode="live",
        started_at_ms=1_000,
        target_quote_volume=Decimal("10"),
    )
    ledger.update_session("first", finished_at_ms=2_000)
    first_fill = NormalizedTradeFill(
        identity="first-fill",
        executed_at_ms=1_500,
        quote_volume=Decimal("4"),
        symbol="BTCUSDT",
        position_action="open",
    )
    later_fill = NormalizedTradeFill(
        identity="later-fill",
        executed_at_ms=3_000,
        quote_volume=Decimal("6"),
        symbol="BTCUSDT",
        position_action="open",
    )
    try:
        ledger.record_account_fills("a3", "live", (first_fill, later_fill))
        projection = service.reconcile("first", (first_fill,), reconciled_at_ms=2_500)
    finally:
        ledger.close()

    assert projection["reconciliation_required"] is False
    assert projection["verified_quote_volume"] == "4"
    assert projection["fill_count"] == 1
