from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest
from pydantic import SecretStr
from weex_cli.core.errors import SafetyError

from fleet_api.auth.vault import CredentialMaterial, EphemeralCredentialVault
from fleet_api.campaigns import (
    CampaignWorkerManager,
    InMemoryCampaignJournal,
    SQLiteCampaignJournal,
    _sanitize_event,
    _worker_exception_reason,
)
from fleet_api.models import ActiveExecutionWait, BetaCampaignPreviewRequest, BetaCampaignStatus
from fleet_api.monitoring.strategy_monitor import StrategyMonitorService
from fleet_api.monitoring.strategy_monitor_actor import ActorLifecycleProjection, merge_actor_waits
from fleet_api.services.control.service import UnsafeOperation
from fleet_api.volume.core.volume_history import InMemoryTradeVolumeLedger, NormalizedTradeFill

from ...support.test_campaigns_support import (
    FakeBetaProvider,
    FakeGateway,
    live_profile,
    live_settings,
    metadata,
    sample_campaign,
)


def test_monitor_replays_complete_legacy_journal_without_event_cap() -> None:
    journal = InMemoryCampaignJournal()
    campaign = sample_campaign()
    journal.create("ins-1", campaign, metadata(campaign))
    for round_number in range(1, 2_105):
        journal.add_event(campaign.campaign_id, _sanitize_event({"event": "cycle_started", "round": round_number}))

    snapshot = StrategyMonitorService(
        journal,
        InMemoryTradeVolumeLedger(),
        "generation-1",
    ).snapshot("ins-1", limit=20)

    assert snapshot.current_round == 2_104
    assert snapshot.projection_sequence == 2_104
    assert snapshot.projection_version == 7
    assert snapshot.stream_state == "ready"


def test_sqlite_monitor_sequences_remain_monotonic_across_connections(tmp_path) -> None:
    path = tmp_path / "fleet.db"
    first = SQLiteCampaignJournal(path)
    second = SQLiteCampaignJournal(path)
    campaign = sample_campaign()
    first.create("ins-1", campaign, {**metadata(campaign), "owner_user_id": "gg"})

    def append(index: int) -> int:
        journal = first if index % 2 else second
        return journal.append_and_project(
            campaign.campaign_id,
            _sanitize_event({"event": "cycle_started", "round": index + 1}),
            owner_user_id="gg",
            account_id="ins-1",
            session_id=None,
            executor_generation="generation-1",
            projection_version=3,
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        sequences = sorted(pool.map(append, range(20)))

    projection, rows, latest = first.monitor_read(campaign.campaign_id, None, 50)
    assert sequences == list(range(1, 21))
    assert [row["sequence"] for row in rows] == sequences
    assert latest == 20
    assert projection is not None
    assert projection.projected_sequence == latest
    first.close()
    second.close()


def test_sqlite_projection_restores_wait_deadline_after_reopen(tmp_path) -> None:
    path = tmp_path / "fleet.db"
    journal = SQLiteCampaignJournal(path)
    campaign = sample_campaign()
    journal.create("ins-1", campaign, {**metadata(campaign), "owner_user_id": "gg"})
    journal.append_and_project(
        campaign.campaign_id,
        _sanitize_event({"event": "hold_started", "round": 1, "seconds": "10"}),
        owner_user_id="gg",
        account_id="ins-1",
        session_id=None,
        executor_generation="generation-1",
        projection_version=3,
    )
    projection = journal.monitor_projection(campaign.campaign_id)
    assert projection is not None
    expected_deadline = projection.state["active_waits"][0]["deadline_at_ms"]
    journal.close()

    reopened = SQLiteCampaignJournal(path)
    snapshot = StrategyMonitorService(reopened, InMemoryTradeVolumeLedger(), "generation-2").snapshot("ins-1")
    assert snapshot.projection_sequence == 1
    assert snapshot.active_waits[0].deadline_at_ms == expected_deadline
    reopened.close()


def test_close_queue_supersedes_expired_hold_projection() -> None:
    waits = [
        ActiveExecutionWait(
            key="hold",
            label="双边持仓计时",
            updated_at_ms=1_000,
            remaining_ms=0,
            deadline_at_ms=1_000,
        ),
        ActiveExecutionWait(key="cycle-stage:1", label="准备平仓", updated_at_ms=1_000),
    ]
    actor = ActorLifecycleProjection(
        execution_state="phase_queued",
        phase="正常阶段排队",
        queue_phase="close",
        queue_position=2,
        estimated_start_at_ms=2_000,
    )

    merged = merge_actor_waits(waits, actor, updated_at_ms=1_100)

    assert [wait.key for wait in merged] == ["actor-phase-queue"]
    assert merged[0].label == "正常平仓阶段排队"


def test_monitor_projects_actor_phase_queue_without_raw_event_names() -> None:
    journal = InMemoryCampaignJournal()
    campaign = sample_campaign()
    journal.create("ins-1", campaign, metadata(campaign))
    journal.add_event(
        campaign.campaign_id,
        _sanitize_event(
            {
                "event": "actor_lifecycle",
                "phase": "phase_queued",
                "queue_phase": "open",
                "queue_position": 3,
                "estimated_start_at_ms": 12_000,
                "queue_constraint": "proxy_cooldown",
            }
        ),
    )

    snapshot = StrategyMonitorService(journal, InMemoryTradeVolumeLedger(), "generation-1").snapshot("ins-1")

    assert snapshot.execution_state == "phase_queued"
    assert snapshot.phase == "正常阶段排队"
    assert snapshot.phase_queue_position == 3
    assert snapshot.phase_queue_estimated_start_at_ms == 12_000
    assert snapshot.phase_queue_proxy_limited is True
    assert snapshot.active_waits[-1].label == "正常开仓阶段排队"
    assert "同代理阶段释放" in snapshot.active_waits[-1].detail
    assert snapshot.timeline[-1].title == "正常阶段排队"
    assert snapshot.timeline[-1].detail.startswith("等待正常开仓槽位")


def test_monitor_complete_ledger_wins_over_execution_journal_projection() -> None:
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
                "quote_volume": "90",
                "total_quote": "90",
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

    snapshot = StrategyMonitorService(journal, ledger, "generation-1").snapshot("ins-1")

    assert snapshot.verified_quote_volume == Decimal("12.50")
    assert snapshot.remaining_quote_volume == Decimal("87.50")
    assert snapshot.volume_source == "ledger"


def test_monitor_missing_legacy_session_stays_available_and_requires_reconciliation() -> None:
    journal = InMemoryCampaignJournal()
    campaign = sample_campaign()
    details = metadata(campaign)
    details["session_id"] = "missing-session"
    journal.create("ins-1", campaign, details)
    journal.add_event(
        campaign.campaign_id,
        _sanitize_event(
            {
                "event": "cycle_completed",
                "round": 1,
                "status": "completed",
                "quote_volume": "12.50",
                "total_quote": "12.50",
            }
        ),
    )

    snapshot = StrategyMonitorService(journal, InMemoryTradeVolumeLedger(), "generation-1").snapshot("ins-1")

    assert snapshot.session_id == "missing-session"
    assert snapshot.reconciliation_required is True
    assert snapshot.stale is True
    assert snapshot.volume_source == "execution_journal"
    assert snapshot.verified_quote_volume == Decimal("12.50")


def test_worker_safety_reason_is_whitelisted_without_persisting_exception_message() -> None:
    assert _worker_exception_reason(SafetyError("available USDT is insufficient for the planned opening budget")) == (
        "worker_safety:available_balance_insufficient"
    )
    assert _worker_exception_reason(SafetyError("proxy password=very-secret")) == "worker_safety:preflight_rejected"


def test_monitor_journal_paginates_without_duplicate_sequences() -> None:
    journal = InMemoryCampaignJournal()
    campaign = sample_campaign()
    journal.create("ins-1", campaign, metadata(campaign))
    for round_number in range(1, 7):
        journal.add_event(
            campaign.campaign_id,
            _sanitize_event({"event": "cycle_started", "round": round_number}),
        )

    newest = journal.events_before(campaign.campaign_id, None, 3)
    older = journal.events_before(campaign.campaign_id, int(newest[0]["sequence"]), 3)

    assert [row["sequence"] for row in newest] == [4, 5, 6]
    assert [row["sequence"] for row in older] == [1, 2, 3]
    assert journal.events_after(campaign.campaign_id, 3, 10) == newest


def test_preview_uses_fake_gateway_and_rejects_non_flat_account(monkeypatch, tmp_path) -> None:
    allocation = sample_campaign().allocation
    gateway = FakeGateway()
    manager = CampaignWorkerManager(
        live_settings(tmp_path),
        EphemeralCredentialVault(),
        InMemoryCampaignJournal(),
        lambda: FakeBetaProvider(allocation),  # type: ignore[arg-type]
    )
    manager._profile_and_gateway = lambda _material: (live_profile(tmp_path), gateway)  # type: ignore[method-assign]
    material = CredentialMaterial(
        api_key=SecretStr("key"),
        api_secret=SecretStr("secret"),
        passphrase=SecretStr("passphrase"),
        proxy_url=SecretStr("proxy:443:user:password"),
    )
    preview = manager.preview(
        "ins-1",
        BetaCampaignPreviewRequest(target_quote=Decimal("6000"), cycle_volume=Decimal("500")),
        material,
    )
    assert preview.status is BetaCampaignStatus.PLANNED
    assert preview.beta == Decimal("0.4")
    assert preview.confirmation.startswith("EXECUTE WEEX LIVE BETA-CAMPAIGN WC-")
    assert gateway.closed
    manager.close()

    blocked_gateway = FakeGateway(positions=True)
    blocked = CampaignWorkerManager(
        live_settings(tmp_path / "blocked"),
        EphemeralCredentialVault(),
        InMemoryCampaignJournal(),
        lambda: FakeBetaProvider(allocation),  # type: ignore[arg-type]
    )
    blocked._profile_and_gateway = lambda _material: (live_profile(tmp_path), blocked_gateway)  # type: ignore[method-assign]
    with pytest.raises(UnsafeOperation, match="account_is_not_flat"):
        blocked.preview(
            "ins-2",
            BetaCampaignPreviewRequest(target_quote=Decimal("6000"), cycle_volume=Decimal("500")),
            material,
        )
    blocked.close()
