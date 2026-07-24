from decimal import Decimal

import pytest

from fleet_api.campaigns import (
    InMemoryCampaignJournal,
    SQLiteCampaignJournal,
    _AccountLease,
    _sanitize_event,
)
from fleet_api.models import BetaCampaignStatus
from fleet_api.service import UnsafeOperation
from fleet_api.strategy_monitor import StrategyMonitorService
from fleet_api.volume_history import InMemoryTradeVolumeLedger, NormalizedTradeFill

from .test_campaigns_support import (
    metadata,
    sample_campaign,
)


def test_in_memory_journal_enforces_one_active_campaign_and_recovers() -> None:
    journal = InMemoryCampaignJournal()
    campaign = sample_campaign()
    journal.create("ins-1", campaign, metadata(campaign))
    with pytest.raises(UnsafeOperation, match="active Beta Campaign"):
        journal.create("ins-1", campaign, metadata(campaign))
    journal.update(campaign.campaign_id, status=BetaCampaignStatus.EXECUTING.value)
    assert journal.recover_incomplete() == 1
    recovered = journal.get(campaign.campaign_id)
    assert recovered is not None
    assert recovered.status == BetaCampaignStatus.RECOVERING.value
    assert recovered.metadata["reason"] == "control_plane_restart"


def test_sqlite_journal_round_trips_campaign_and_events(tmp_path) -> None:
    journal = SQLiteCampaignJournal(tmp_path / "fleet.db")
    campaign = sample_campaign()
    journal.create("ins-1", campaign, metadata(campaign))
    journal.add_event(campaign.campaign_id, {"sequence": 1, "name": "campaign_started", "at_ms": 1})
    record = journal.get(campaign.campaign_id)
    assert record is not None
    assert record.campaign == campaign
    assert record.events[0]["name"] == "campaign_started"
    journal.close()


def test_sqlite_execution_claim_is_atomic_across_journal_connections(tmp_path) -> None:
    path = tmp_path / "fleet.db"
    first = SQLiteCampaignJournal(path)
    second = SQLiteCampaignJournal(path)
    campaign = sample_campaign()
    first.create("ins-1", campaign, metadata(campaign))

    assert first.claim_execution(campaign.campaign_id, started_at_ms=1_000) is True
    assert second.claim_execution(campaign.campaign_id, started_at_ms=1_001) is False
    claimed = second.get(campaign.campaign_id)
    assert claimed is not None
    assert claimed.status == BetaCampaignStatus.EXECUTING.value
    assert claimed.metadata["started_at_ms"] == 1_000
    first.close()
    second.close()


def test_account_lease_blocks_duplicate_api_key_across_instances(tmp_path) -> None:
    first = _AccountLease(tmp_path, "same-api-key", "ins-1", "wc-first")
    second = _AccountLease(tmp_path, "same-api-key", "ins-2", "wc-second")
    other = _AccountLease(tmp_path, "different-api-key", "ins-3", "wc-third")
    first.acquire()
    try:
        with pytest.raises(UnsafeOperation, match="already in use"):
            second.acquire()
        other.acquire()
        other.release()
        payload = first.path.read_text(encoding="utf-8")
        assert "same-api-key" not in payload
        assert "ins-1" in payload
    finally:
        first.release()


def test_journal_assigns_monotonic_event_sequences() -> None:
    journal = InMemoryCampaignJournal()
    campaign = sample_campaign()
    journal.create("ins-1", campaign, metadata(campaign))
    assert journal.add_event(campaign.campaign_id, {"sequence": 99, "name": "first"}) == 1
    assert journal.add_event(campaign.campaign_id, {"name": "second"}) == 2
    record = journal.get(campaign.campaign_id)
    assert record is not None
    assert [event["sequence"] for event in record.events] == [1, 2]


def test_monitor_event_sanitizer_keeps_progress_fields_without_identifiers_or_secrets() -> None:
    event = _sanitize_event(
        {
            "event": "leg_progress",
            "sequence": 7,
            "symbol": "BTCUSDT",
            "action": "open",
            "progress_event": "wait",
            "waiting_for": "maker_fill",
            "price": Decimal("70000.1000"),
            "quantity": Decimal("0.0010"),
            "elapsed_ms": 1_250,
            "remaining_ms": 8_750,
            "order_id": "exchange-order-secret",
            "client_order_id": "client-order-secret",
            "api_secret": "api-secret",
            "passphrase": "passphrase",
            "proxy_password": "proxy-password",
            "raw_response": {"sensitive": True},
        }
    )

    assert event["fields"] == {
        "leg_sequence": 7,
        "symbol": "BTCUSDT",
        "action": "open",
        "progress_event": "wait",
        "waiting_for": "maker_fill",
        "price": "70000.1000",
        "quantity": "0.0010",
        "elapsed_ms": 1_250,
        "remaining_ms": 8_750,
    }
    serialized = str(event)
    assert "exchange-order-secret" not in serialized
    assert "client-order-secret" not in serialized
    assert "api-secret" not in serialized
    assert "proxy-password" not in serialized


def test_monitor_uses_authoritative_session_ledger_not_planned_event_amounts() -> None:
    journal = InMemoryCampaignJournal()
    campaign = sample_campaign()
    details = metadata(campaign)
    details["session_id"] = "session-1"
    journal.create("ins-1", campaign, details)
    journal.add_event(
        campaign.campaign_id,
        _sanitize_event(
            {
                "event": "cycle_started",
                "round": 1,
                "desired_quote": "999999",
                "btc_quantity": "1",
                "eth_quantity": "1",
                "leverage": 6,
            }
        ),
    )
    journal.add_event(
        campaign.campaign_id,
        _sanitize_event(
            {
                "event": "leg_completed",
                "round": 1,
                "sequence": 1,
                "symbol": "BTCUSDT",
                "action": "open",
                "quote_volume": "12.50",
                "fill_count": 1,
            }
        ),
    )
    ledger = InMemoryTradeVolumeLedger()
    ledger.create_session("session-1", "ins-1", "live", 1_000, Decimal("100"))
    ledger.record_account_fills(
        "ins-1",
        "live",
        (
            NormalizedTradeFill(
                identity="fill-1",
                executed_at_ms=1_100,
                quote_volume=Decimal("12.50"),
                symbol="BTCUSDT",
                position_action="open",
                maker=True,
                authoritative=True,
            ),
        ),
    )
    ledger.update_session("session-1", source_complete=True, stale=False, pending_sync=False)

    monitor = StrategyMonitorService(journal, ledger, "generation-1")
    snapshot = monitor.snapshot("ins-1")
    progress = monitor.progress_for_session("ins-1", "session-1")

    assert snapshot.target_quote_volume == Decimal("100")
    assert snapshot.verified_quote_volume == Decimal("12.50")
    assert snapshot.remaining_quote_volume == Decimal("87.50")
    assert snapshot.btc_quote_volume == Decimal("12.50")
    assert snapshot.eth_quote_volume == 0
    assert snapshot.maker_fill_count == 1
    assert all("999999" not in entry.title for entry in snapshot.timeline)
    assert progress is not None
    assert progress.verified_quote_volume == Decimal("12.50")
    assert progress.volume_source == "ledger"


def test_monitor_projects_reconciled_leg_events_while_session_ledger_is_pending() -> None:
    journal = InMemoryCampaignJournal()
    campaign = sample_campaign()
    details = metadata(campaign)
    details["session_id"] = "session-1"
    journal.create("ins-1", campaign, details)
    for payload in (
        {"event": "campaign_run_started", "run": 1, "remaining_quote": "100"},
        {
            "event": "leg_completed",
            "run": 1,
            "round": 1,
            "sequence": 1,
            "symbol": "BTCUSDT",
            "action": "open",
            "quote_volume": "30.25",
            "fill_count": 1,
        },
        {
            "event": "leg_completed",
            "run": 1,
            "round": 1,
            "sequence": 2,
            "symbol": "ETHUSDT",
            "action": "open",
            "quote_volume": "10.75",
            "fill_count": 1,
        },
        {
            "event": "cycle_completed",
            "run": 1,
            "round": 1,
            "status": "completed",
            "quote_volume": "82.00",
            "total_quote": "82.00",
        },
    ):
        journal.add_event(campaign.campaign_id, _sanitize_event(payload))
    ledger = InMemoryTradeVolumeLedger()
    ledger.create_session("session-1", "ins-1", "live", 1_000, Decimal("100"))

    monitor = StrategyMonitorService(journal, ledger, "generation-1")
    snapshot = monitor.snapshot("ins-1")
    progress = monitor.progress_for_session("ins-1", "session-1")

    # The journal must not use planned cycle amounts, but each leg_completed
    # event has already reconciled actual fills and can drive the live view
    # while the wider history ledger catches up.
    assert snapshot.verified_quote_volume == Decimal("41.00")
    assert snapshot.ledger_verified_quote_volume == 0
    assert snapshot.remaining_quote_volume == Decimal("59.00")
    assert snapshot.btc_quote_volume == Decimal("30.25")
    assert snapshot.eth_quote_volume == Decimal("10.75")
    assert snapshot.maker_fill_count == 0
    assert snapshot.taker_fill_count == 0
    assert snapshot.unknown_fill_count == 2
    assert snapshot.volume_source == "execution_journal"
    assert snapshot.source_complete is False
    assert snapshot.stale is True
    assert progress is not None
    assert progress.verified_quote_volume == Decimal("41.00")
    assert progress.volume_source == "execution_journal"


def test_monitor_keeps_actor_phase_when_a_delta_has_no_actor_lifecycle_event() -> None:
    journal = InMemoryCampaignJournal()
    campaign = sample_campaign()
    journal.create("ins-1", campaign, metadata(campaign))
    journal.add_event(
        campaign.campaign_id,
        {
            "event": "actor_lifecycle",
            "phase": "phase_queued",
            "queue_phase": "open",
            "queue_position": 3,
            "estimated_start_at_ms": 12_000,
            "at_ms": 1_000,
        },
    )
    journal.add_event(
        campaign.campaign_id,
        {"event": "leg_preparing", "symbol": "BTCUSDT", "action": "open", "at_ms": 1_500},
    )

    snapshot = StrategyMonitorService(journal, InMemoryTradeVolumeLedger(), "generation-1").snapshot(
        "ins-1",
        event_rows=journal.events_after(campaign.campaign_id, 1, 1),
    )

    assert snapshot.execution_state == "phase_queued"
    assert snapshot.phase_queue_position == 3
    assert any(wait.key == "actor-phase-queue" for wait in snapshot.active_waits)
    assert all(entry.event_name != "actor_lifecycle" for entry in snapshot.timeline)


def test_sqlite_event_and_projection_commit_atomically(tmp_path) -> None:
    journal = SQLiteCampaignJournal(tmp_path / "fleet.db")
    campaign = sample_campaign()
    details = metadata(campaign)
    details.update({"owner_user_id": "gg", "session_id": "session-1"})
    journal.create("ins-1", campaign, details)

    with pytest.raises(TypeError):
        journal.append_and_project(
            campaign.campaign_id,
            {"name": "campaign_run_started", "at_ms": 1_000},
            owner_user_id="gg",
            account_id="ins-1",
            session_id="session-1",
            executor_generation="generation-1",
            state={"not_json": object()},
            projection_version=3,
        )

    projection, rows, latest = journal.monitor_read(campaign.campaign_id, None, 10)
    assert projection is None
    assert rows == []
    assert latest == 0

    sequence = journal.append_and_project(
        campaign.campaign_id,
        {"name": "campaign_run_started", "at_ms": 1_001, "run": 1},
        owner_user_id="gg",
        account_id="ins-1",
        session_id="session-1",
        executor_generation="generation-1",
        state={"schema_version": 3, "phase": "运行开始", "current_run": 1, "active_waits": []},
        projection_version=3,
    )

    projection, rows, latest = journal.monitor_read(campaign.campaign_id, None, 10)
    assert sequence == latest == 1
    assert projection is not None
    assert projection.projected_sequence == 1
    assert projection.owner_user_id == "gg"
    assert rows[0]["sequence"] == 1
    assert journal.monitor_metrics()["transaction_failures"] == 1
    journal.close()
