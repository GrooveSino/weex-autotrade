from decimal import Decimal

from fleet_api.campaign_events import _sanitize_event
from fleet_api.campaigns import InMemoryCampaignJournal
from fleet_api.strategy_monitor import StrategyMonitorService
from fleet_api.volume_history import InMemoryTradeVolumeLedger

from .test_campaigns_support import metadata, sample_campaign


def _monitor_with_session(**session_updates):  # type: ignore[no-untyped-def]
    journal = InMemoryCampaignJournal()
    campaign = sample_campaign()
    details = metadata(campaign)
    details["session_id"] = "session-1"
    journal.create("ins-1", campaign, details)
    journal.add_event(
        campaign.campaign_id,
        _sanitize_event(
            {
                "event": "cycle_completed",
                "round": 1,
                "status": "completed",
                "quote_volume": "67.2638",
                "total_quote": "67.2638",
            }
        ),
    )
    ledger = InMemoryTradeVolumeLedger()
    ledger.create_session("session-1", "ins-1", "live", 1_000, Decimal("500"))
    ledger.update_session("session-1", **session_updates)
    return journal, campaign, StrategyMonitorService(journal, ledger, "generation-1")


def test_monitor_reports_ledger_queue_only_when_pending_sync_is_true() -> None:
    _journal, _campaign, monitor = _monitor_with_session(
        source_complete=False,
        stale=True,
        pending_sync=True,
        audit_status="pending",
    )

    snapshot = monitor.snapshot("ins-1")

    assert snapshot.ledger_sync_state == "queued"
    assert snapshot.audit_status == "pending"
    assert snapshot.volume_source == "execution_journal"


def test_monitor_separates_complete_ledger_from_pending_audit() -> None:
    _journal, _campaign, monitor = _monitor_with_session(
        source_complete=True,
        stale=False,
        pending_sync=False,
        audit_status="pending",
    )

    snapshot = monitor.snapshot("ins-1")

    assert snapshot.ledger_sync_state == "complete"
    assert snapshot.audit_status == "pending"
    assert snapshot.volume_source == "ledger"


def test_monitor_exposes_owned_recovery_boundary_without_generic_typeerror() -> None:
    journal, campaign, monitor = _monitor_with_session(
        source_complete=True,
        stale=False,
        pending_sync=False,
        audit_status="pending",
    )
    journal.update(
        campaign.campaign_id,
        status="recovering",
        recovery_state="cleanup_required",
        recovery_boundary_state="owned_exposure",
        recovery_attempt=2,
    )

    snapshot = monitor.snapshot("ins-1")

    assert snapshot.boundary_state == "owned_exposure"
    assert snapshot.recovery_state == "cleanup_required"
    assert snapshot.recovery_attempt == 2
    assert snapshot.phase == "当前任务仓位待安全收尾"
