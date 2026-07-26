from __future__ import annotations

from decimal import Decimal

from fleet_api.campaigns import InMemoryCampaignJournal, SQLiteCampaignJournal

from ...support.test_campaigns_support import metadata, sample_campaign


def _exercise(journal: InMemoryCampaignJournal | SQLiteCampaignJournal) -> None:
    campaign = sample_campaign()
    details = metadata(campaign)
    details.update({"owner_user_id": "gg", "session_id": "session-1"})
    journal.create("ins-1", campaign, details)
    journal.update(campaign.campaign_id, result={"total": Decimal("12.34")}, decimal_value=Decimal("1.20"))
    journal.add_event(campaign.campaign_id, {"name": "final", "at_ms": 1_000, "quote": Decimal("12.34")})
    journal.append_and_project(
        campaign.campaign_id,
        {"name": "final", "at_ms": 1_001, "quote": Decimal("12.34")},
        owner_user_id="gg",
        account_id="ins-1",
        session_id="session-1",
        executor_generation="generation-1",
        state={"phase": "completed", "current_run": 1, "amount": Decimal("12.34"), "active_waits": []},
        projection_version=1,
    )
    journal.replace_boundary_projection("ins-1", {"quote": Decimal("12.34")})
    record = journal.get(campaign.campaign_id)
    projection = journal.monitor_projection(campaign.campaign_id)
    assert record is not None
    assert record.result == {"total": "12.34"}
    assert record.metadata["decimal_value"] == "1.20"
    assert record.events[0]["quote"] == "12.34"
    assert projection is not None
    assert projection.state["amount"] == "12.34"
    assert journal.boundary_projection("ins-1") == {"quote": "12.34"}


def test_in_memory_campaign_journal_normalizes_decimal_execution_output() -> None:
    _exercise(InMemoryCampaignJournal())


def test_sqlite_campaign_journal_normalizes_decimal_execution_output(tmp_path) -> None:
    journal = SQLiteCampaignJournal(tmp_path / "fleet.db")
    try:
        _exercise(journal)
    finally:
        journal.close()
