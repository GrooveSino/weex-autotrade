import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import SecretStr
from weex_cli.beta_allocation import BetaAllocation, BetaUnavailable
from weex_cli.beta_campaign import (
    BetaVolumeCampaign,
    _selected_round_turnover,
    campaign_confirmation,
    live_profile_fingerprint,
)
from weex_cli.config import Credentials, Settings
from weex_cli.live_profile import LiveProfile

from fleet_api.campaign_log import campaign_event_log
from fleet_api.campaigns import (
    CampaignWorkerManager,
    InMemoryCampaignJournal,
    SQLiteCampaignJournal,
    _AccountLease,
    _sanitize_event,
)
from fleet_api.config import ControlPlaneSettings
from fleet_api.models import BetaCampaignPreviewRequest, BetaCampaignStatus, VolumeStrategy
from fleet_api.service import BetaSourceUnavailable, UnsafeOperation
from fleet_api.strategy_monitor import StrategyMonitorService
from fleet_api.vault import CredentialMaterial, EphemeralCredentialVault
from fleet_api.volume_history import InMemoryTradeVolumeLedger, NormalizedTradeFill


class FakeGateway:
    def __init__(self, *, available: str = "1000", positions: bool = False) -> None:
        self.available = available
        self.positions_non_empty = positions
        self.children: list[FakeGateway] = []
        self.closed = False

    def order_book(self, symbol: str, _limit: int = 5) -> dict[str, object]:
        return {"bids": [["100", "10"]], "asks": [["101", "10"]] if symbol == "BTC" else [["101", "10"]]}

    def amount_step(self, _symbol: str) -> Decimal:
        return Decimal("0.001")

    def amount_to_precision(self, _symbol: str, amount: Decimal) -> Decimal:
        return amount.quantize(Decimal("0.001"))

    def account_balance_rows(self, _mode: str) -> list[dict[str, str]]:
        return [{"asset": "USDT", "availableBalance": self.available}]

    def positions(self, _mode: str, _symbol: str) -> list[dict[str, str]]:
        return [{"size": "1", "side": "long"}] if self.positions_non_empty else []

    def open_orders(self, _symbol: str, *, mode: str = "live") -> list[dict[str, str]]:
        return []

    def algo_orders(self, _symbol: str) -> list[dict[str, str]]:
        return []

    def fork(self) -> "FakeGateway":
        child = FakeGateway(available=self.available, positions=self.positions_non_empty)
        self.children.append(child)
        return child

    def close(self) -> None:
        self.closed = True


class FakeBetaProvider:
    def __init__(self, allocation: BetaAllocation) -> None:
        self.allocation = allocation

    def get(self) -> BetaAllocation:
        return self.allocation


class UnavailableBetaProvider:
    def get(self) -> BetaAllocation:
        raise BetaUnavailable("beta_request_failed:httperror")


def live_settings(tmp_path, *, workers: int = 1) -> ControlPlaneSettings:
    return ControlPlaneSettings(
        adapter="weex-live",
        storage="sqlite",
        master_key=SecretStr("not-used-by-memory-journal"),
        live_campaigns_enabled=True,
        live_trading_enabled=True,
        live_campaign_worker_count=workers,
        campaign_data_directory=tmp_path / "campaign-data",
    )


def live_profile(tmp_path: Path) -> LiveProfile:
    return LiveProfile(
        path=tmp_path / "profile.toml",
        settings=Settings(
            credentials=Credentials("key", "secret", "passphrase"),
            default_mode="live",
            live_trading_enabled=True,
        ),
        proxy_url="https://user:password@example.test:443",
        allow_live_mutations=True,
        post_only_only=True,
    )


def sample_campaign() -> BetaVolumeCampaign:
    btc_weight = Decimal(1) / (Decimal(1) + Decimal("0.4"))
    allocation = BetaAllocation(
        beta=Decimal("0.4"),
        btc_long_weight=btc_weight,
        eth_short_weight=Decimal(1) - btc_weight,
        version="beta-v1:1",
        as_of_ms=1,
        confidence=Decimal("0.5"),
        confidence_threshold=Decimal("0.65"),
        source="fake",
    )
    return BetaVolumeCampaign(
        schema_version=2,
        campaign_id="wc-ABCDEF1234",
        created_at_ms=1,
        expires_at_ms=10_000,
        profile_fingerprint="f" * 64,
        target_turnover_quote=Decimal("6000"),
        round_turnover_quote_min=Decimal("500"),
        round_turnover_quote=Decimal("500"),
        max_position_quote=Decimal("1200"),
        timeout_seconds=60,
        recovery_attempts=3,
        max_empty_rounds=3,
        cooldown_seconds=0.0,
        hold_min_seconds=300.0,
        hold_max_seconds=420.0,
        round_gap_min_seconds=300.0,
        round_gap_max_seconds=420.0,
        max_runs=20,
        leverage="auto",
        max_auto_leverage=99,
        margin_buffer=Decimal("1.2"),
        margin_mode="isolated",
        allocation=allocation,
    )._with_computed_id()


def metadata(campaign: BetaVolumeCampaign) -> dict[str, object]:
    return {
        "confirmation": campaign_confirmation(campaign),
        "stop_confirmation": f"STOP WEEX LIVE BETA-CAMPAIGN {campaign.campaign_id.upper()} POST_ONLY",
        "available_quote": "100",
        "required_leverage": 6,
        "planned_leverage": 6,
        "max_supported_turnover_quote": "16500",
    }


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
    assert recovered.status == BetaCampaignStatus.UNCERTAIN.value
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

    snapshot = StrategyMonitorService(journal, ledger, "generation-1").snapshot("ins-1")

    assert snapshot.target_quote_volume == Decimal("100")
    assert snapshot.verified_quote_volume == Decimal("12.50")
    assert snapshot.remaining_quote_volume == Decimal("87.50")
    assert snapshot.btc_quote_volume == Decimal("12.50")
    assert snapshot.eth_quote_volume == 0
    assert snapshot.maker_fill_count == 1
    assert all("999999" not in entry.title for entry in snapshot.timeline)


def test_monitor_never_promotes_execution_event_totals_while_session_ledger_is_pending() -> None:
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

    snapshot = StrategyMonitorService(journal, ledger, "generation-1").snapshot("ins-1")

    assert snapshot.verified_quote_volume == Decimal("0")
    assert snapshot.ledger_verified_quote_volume == 0
    assert snapshot.remaining_quote_volume == Decimal("100")
    assert snapshot.btc_quote_volume == Decimal("0")
    assert snapshot.eth_quote_volume == Decimal("0")
    assert snapshot.volume_source == "pending"
    assert snapshot.source_complete is False
    assert snapshot.stale is True


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
    assert snapshot.projection_version == 3
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


def test_worker_uses_independent_lane_gateways_and_records_events(monkeypatch, tmp_path) -> None:
    allocation = sample_campaign().allocation
    gateway = FakeGateway()
    progress_events: list[tuple[str, dict[str, object]]] = []
    manager = CampaignWorkerManager(
        live_settings(tmp_path),
        EphemeralCredentialVault(),
        InMemoryCampaignJournal(),
        lambda: FakeBetaProvider(allocation),  # type: ignore[arg-type]
        on_progress=lambda instance_id, event: progress_events.append((instance_id, dict(event))),
    )
    profile = live_profile(tmp_path)
    manager._profile_and_gateway = lambda _material: (profile, gateway)  # type: ignore[method-assign]

    class FakeWebSocket:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def start(self) -> None:
            return None

        def close(self) -> None:
            return None

    captured: dict[str, object] = {}

    class FakeCampaignService:
        def __init__(self, primary, _provider, _campaign_store, _child_store, **kwargs) -> None:
            captured["primary"] = primary
            captured["lanes"] = kwargs["lane_gateways"]
            self.event_sink = kwargs["event_sink"]

        def execute(self, _campaign):
            self.event_sink({"event": "campaign_run_started", "run": 1})
            captured["primary"].available = "999.75"
            return {
                "status": "completed",
                "executed_quote_volume": "500",
                "remaining_quote": "0",
                "excess_quote": "0",
                "maker_only": True,
            }

    monkeypatch.setattr("fleet_api.campaigns.WeexCampaignWebSocketRuntime", FakeWebSocket)
    monkeypatch.setattr("fleet_api.campaigns.LiveBetaVolumeCampaignService", FakeCampaignService)
    material = CredentialMaterial(
        api_key=SecretStr("key"),
        api_secret=SecretStr("secret"),
        passphrase=SecretStr("passphrase"),
        proxy_url=SecretStr("proxy:443:user:password"),
    )
    profile = live_profile(tmp_path)
    now_ms = int(time.time() * 1000)
    campaign = replace(
        sample_campaign(),
        created_at_ms=now_ms,
        expires_at_ms=now_ms + 3_600_000,
        profile_fingerprint=live_profile_fingerprint(profile),
    )._with_computed_id()
    manager.journal.create("ins-1", campaign, metadata(campaign))
    manager._verify_execution_boundary = lambda _record, _material: Decimal("1000")  # type: ignore[method-assign]
    with pytest.raises(UnsafeOperation, match="risk acknowledgement"):
        manager.start("ins-1", campaign.campaign_id, str(metadata(campaign)["confirmation"]), False, material)
    with pytest.raises(UnsafeOperation, match="exact campaign confirmation"):
        manager.start("ins-1", campaign.campaign_id, "wrong", True, material)
    manager.start("ins-1", campaign.campaign_id, str(metadata(campaign)["confirmation"]), True, material)
    manager._futures[campaign.campaign_id].result(timeout=3)
    record = manager.journal.get(campaign.campaign_id)
    assert record is not None
    assert record.status == BetaCampaignStatus.COMPLETED.value
    assert record.metadata["current_run"] == 1
    assert record.metadata["starting_available_balance_quote"] == "1000"
    assert record.metadata["ending_available_balance_quote"] == "999.75"
    assert [event["sequence"] for event in record.events] == [1]
    assert progress_events == [("ins-1", record.events[0])]
    lanes = captured["lanes"]
    assert isinstance(lanes, dict)
    assert lanes["BTC"] is not lanes["ETH"]
    assert lanes["BTC"] is not captured["primary"]
    manager.close()


def test_campaign_progress_formatter_is_safe_and_keeps_verified_fill_context() -> None:
    level, message = campaign_event_log(
        {
            "name": "leg_completed",
            "fields": {
                "symbol": "BTCUSDT",
                "action": "open",
                "quote_volume": "250.50",
                "fill_count": 2,
                "api_secret": "must-not-render",
            },
        }
    )

    assert level.value == "success"
    assert message == "实盘执行：BTCUSDT open 成交已核验；250.50 USDT / 2 笔"
    assert "must-not-render" not in message


def test_progress_and_end_balance_failures_do_not_change_worker_result(monkeypatch, tmp_path) -> None:
    allocation = sample_campaign().allocation

    class EndingBalanceFailureGateway(FakeGateway):
        balance_reads = 0

        def account_balance_rows(self, _mode: str) -> list[dict[str, str]]:
            self.balance_reads += 1
            raise TimeoutError("fake balance timeout")

    gateway = EndingBalanceFailureGateway()
    manager = CampaignWorkerManager(
        live_settings(tmp_path),
        EphemeralCredentialVault(),
        InMemoryCampaignJournal(),
        lambda: FakeBetaProvider(allocation),  # type: ignore[arg-type]
        on_progress=lambda _instance_id, _event: (_ for _ in ()).throw(RuntimeError("log unavailable")),
    )
    profile = live_profile(tmp_path)
    manager._profile_and_gateway = lambda _material: (profile, gateway)  # type: ignore[method-assign]

    class FakeWebSocket:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def start(self) -> None:
            return None

        def close(self) -> None:
            return None

    class FakeCampaignService:
        def __init__(self, *_args, **kwargs) -> None:
            self.event_sink = kwargs["event_sink"]

        def execute(self, _campaign):
            self.event_sink({"event": "campaign_run_started", "run": 1, "remaining_quote": "500"})
            return {"status": "completed", "executed_quote_volume": "500", "remaining_quote": "0", "excess_quote": "0"}

    monkeypatch.setattr("fleet_api.campaigns.WeexCampaignWebSocketRuntime", FakeWebSocket)
    monkeypatch.setattr("fleet_api.campaigns.LiveBetaVolumeCampaignService", FakeCampaignService)
    material = CredentialMaterial(
        api_key=SecretStr("key"),
        api_secret=SecretStr("secret"),
        passphrase=SecretStr("passphrase"),
        proxy_url=None,
    )
    now_ms = int(time.time() * 1000)
    campaign = replace(
        sample_campaign(),
        created_at_ms=now_ms,
        expires_at_ms=now_ms + 3_600_000,
        profile_fingerprint=live_profile_fingerprint(profile),
    )._with_computed_id()
    manager.journal.create("ins-1", campaign, metadata(campaign))
    manager._verify_execution_boundary = lambda _record, _material: Decimal("1000")  # type: ignore[method-assign]
    manager.start("ins-1", campaign.campaign_id, str(metadata(campaign)["confirmation"]), True, material)
    manager._futures[campaign.campaign_id].result(timeout=3)

    record = manager.journal.get(campaign.campaign_id)
    assert record is not None
    assert record.status == BetaCampaignStatus.COMPLETED.value
    assert record.metadata["starting_available_balance_quote"] == "1000"
    assert record.metadata["ending_available_balance_quote"] is None
    assert gateway.balance_reads == 1
    assert [event["name"] for event in record.events] == ["campaign_run_started"]
    manager.close()


def test_worker_initialization_failure_becomes_uncertain(monkeypatch, tmp_path) -> None:
    manager = CampaignWorkerManager(
        live_settings(tmp_path),
        EphemeralCredentialVault(),
        InMemoryCampaignJournal(),
        lambda: FakeBetaProvider(sample_campaign().allocation),  # type: ignore[arg-type]
    )
    profile = live_profile(tmp_path)
    now_ms = int(time.time() * 1000)
    campaign = replace(
        sample_campaign(),
        created_at_ms=now_ms,
        expires_at_ms=now_ms + 3_600_000,
        profile_fingerprint=live_profile_fingerprint(profile),
    )._with_computed_id()
    manager.journal.create("ins-1", campaign, metadata(campaign))
    manager._verify_execution_boundary = lambda _record, _material: Decimal("1000")  # type: ignore[method-assign]
    manager._profile_and_gateway = lambda _material: (_ for _ in ()).throw(RuntimeError("gateway failed"))  # type: ignore[method-assign]
    material = CredentialMaterial(
        api_key=SecretStr("key"),
        api_secret=SecretStr("secret"),
        passphrase=SecretStr("passphrase"),
        proxy_url=SecretStr("proxy:443:user:password"),
    )
    manager.start("ins-1", campaign.campaign_id, str(metadata(campaign)["confirmation"]), True, material)
    manager._futures[campaign.campaign_id].result(timeout=3)
    record = manager.journal.get(campaign.campaign_id)
    assert record is not None
    assert record.status == BetaCampaignStatus.UNCERTAIN.value
    assert record.metadata["reason"] == "worker_exception:runtimeerror"
    assert record.events[0]["sequence"] == 1
    manager.close()


def test_start_rechecks_flat_boundary_before_worker_submission(tmp_path) -> None:
    manager = CampaignWorkerManager(
        live_settings(tmp_path),
        EphemeralCredentialVault(),
        InMemoryCampaignJournal(),
        lambda: FakeBetaProvider(sample_campaign().allocation),  # type: ignore[arg-type]
    )
    profile = live_profile(tmp_path)
    now_ms = int(time.time() * 1000)
    campaign = replace(
        sample_campaign(),
        created_at_ms=now_ms,
        expires_at_ms=now_ms + 3_600_000,
        profile_fingerprint=live_profile_fingerprint(profile),
    )._with_computed_id()
    manager.journal.create("ins-1", campaign, metadata(campaign))
    gateway = FakeGateway(positions=True)
    manager._profile_and_gateway = lambda _material: (profile, gateway)  # type: ignore[method-assign]
    material = CredentialMaterial(
        api_key=SecretStr("key"),
        api_secret=SecretStr("secret"),
        passphrase=SecretStr("passphrase"),
        proxy_url=SecretStr("proxy:443:user:password"),
    )

    with pytest.raises(UnsafeOperation, match="no longer flat"):
        manager.start("ins-1", campaign.campaign_id, str(metadata(campaign)["confirmation"]), True, material)

    record = manager.journal.get(campaign.campaign_id)
    assert record is not None
    assert record.status == BetaCampaignStatus.PLANNED.value
    assert campaign.campaign_id not in manager._futures
    assert gateway.closed
    manager.close()


def test_uncertain_campaign_blocks_new_preview_until_flat_boundary_is_acknowledged(tmp_path) -> None:
    manager = CampaignWorkerManager(
        live_settings(tmp_path),
        EphemeralCredentialVault(),
        InMemoryCampaignJournal(),
        lambda: FakeBetaProvider(sample_campaign().allocation),  # type: ignore[arg-type]
    )
    profile = live_profile(tmp_path)
    campaign = replace(
        sample_campaign(),
        profile_fingerprint=live_profile_fingerprint(profile),
    )._with_computed_id()
    manager.journal.create("ins-1", campaign, metadata(campaign))
    manager.journal.update(campaign.campaign_id, status=BetaCampaignStatus.UNCERTAIN.value)
    gateway = FakeGateway()
    manager._profile_and_gateway = lambda _material: (profile, gateway)  # type: ignore[method-assign]
    material = CredentialMaterial(
        api_key=SecretStr("key"),
        api_secret=SecretStr("secret"),
        passphrase=SecretStr("passphrase"),
        proxy_url=SecretStr("proxy:443:user:password"),
    )

    with pytest.raises(UnsafeOperation, match="requires manual reconciliation"):
        manager.preview(
            "ins-1",
            BetaCampaignPreviewRequest(target_quote=Decimal("6000"), cycle_volume=Decimal("500")),
            material,
        )
    with pytest.raises(UnsafeOperation, match="exact reconciliation"):
        manager.reconcile("ins-1", campaign.campaign_id, "wrong", material)

    view = manager.get("ins-1", campaign.campaign_id)
    assert view.reconciliation_required is True
    assert view.retry_allowed is False
    assert view.reconciliation_confirmation is not None
    reconciled = manager.reconcile(
        "ins-1",
        campaign.campaign_id,
        view.reconciliation_confirmation,
        material,
    )
    assert reconciled.status is BetaCampaignStatus.UNCERTAIN
    assert reconciled.reconciliation_required is False
    assert reconciled.reconciliation_confirmation is None
    assert reconciled.events[-1].name == "campaign_reconciliation_acknowledged"
    manager.close()


def test_manager_keeps_live_campaigns_disabled_by_default() -> None:
    settings = ControlPlaneSettings(seed_demo_data=False)
    manager = CampaignWorkerManager(settings, EphemeralCredentialVault(), InMemoryCampaignJournal(), lambda: None)  # type: ignore[arg-type]
    with pytest.raises(UnsafeOperation, match="disabled"):
        manager.preview(
            "ins-1",
            BetaCampaignPreviewRequest(target_quote=Decimal("6000"), cycle_volume=Decimal("500")),
            None,
        )
    manager.close()


def test_campaign_payload_never_contains_credential_material() -> None:
    material = {
        "api_key": SecretStr("key"),
        "api_secret": SecretStr("secret"),
        "passphrase": SecretStr("pass"),
    }
    assert all(value.get_secret_value() not in str(metadata(sample_campaign())) for value in material.values())


def test_bound_strategy_preview_uses_persisted_range_and_read_only_snapshot(tmp_path) -> None:
    manager = CampaignWorkerManager(
        live_settings(tmp_path),
        EphemeralCredentialVault(),
        InMemoryCampaignJournal(),
        lambda: FakeBetaProvider(sample_campaign().allocation),  # type: ignore[arg-type]
    )
    profile = live_profile(tmp_path)
    gateway = FakeGateway()
    manager._profile_and_gateway = lambda _material: (profile, gateway)  # type: ignore[method-assign]
    strategy = VolumeStrategy(
        id="strategy-bound",
        name="Shared Live Range",
        target_volume_quote=Decimal("5000"),
        round_turnover_quote_min=Decimal("220"),
        round_turnover_quote_max=Decimal("480"),
        position_hold_min_seconds=7,
        position_hold_max_seconds=9,
        round_interval_min_seconds=11,
        round_interval_max_seconds=13,
    )
    material = CredentialMaterial(
        api_key=SecretStr("key"),
        api_secret=SecretStr("secret"),
        passphrase=SecretStr("passphrase"),
        proxy_url=None,
    )
    preview = manager.preview_bound_strategy(
        "ins-1",
        strategy,
        Decimal("1250"),
        material,
        session_id="session-bound",
    )
    record = manager.journal.get(preview.campaign_id)
    assert record is not None
    assert preview.strategy_id == strategy.id
    assert preview.strategy_name == strategy.name
    assert preview.strategy_version == 1
    assert preview.round_turnover_quote_min == Decimal("220")
    assert preview.cycle_volume == Decimal("480")
    assert record.metadata["session_id"] == "session-bound"
    assert record.metadata["strategy_snapshot"]["roundTurnoverQuoteMin"] == "220"  # type: ignore[index]
    assert record.metadata["strategy_version"] == 1
    selected = _selected_round_turnover(record.campaign, Decimal("1250"), 2)
    assert Decimal("220") <= selected <= Decimal("480")
    assert selected == _selected_round_turnover(record.campaign, Decimal("1250"), 2)
    assert "STRATEGY" in preview.confirmation
    manager.close()


def test_bound_strategy_preview_reports_beta_source_unavailable_without_creating_a_campaign(tmp_path) -> None:
    journal = InMemoryCampaignJournal()
    manager = CampaignWorkerManager(
        live_settings(tmp_path),
        EphemeralCredentialVault(),
        journal,
        lambda: UnavailableBetaProvider(),  # type: ignore[arg-type]
    )
    profile = live_profile(tmp_path)
    gateway = FakeGateway()
    manager._profile_and_gateway = lambda _material: (profile, gateway)  # type: ignore[method-assign]
    strategy = VolumeStrategy(
        id="strategy-bound",
        name="Shared Live Range",
        target_volume_quote=Decimal("5000"),
        round_turnover_quote_min=Decimal("220"),
        round_turnover_quote_max=Decimal("480"),
        position_hold_min_seconds=7,
        position_hold_max_seconds=9,
        round_interval_min_seconds=11,
        round_interval_max_seconds=13,
    )
    material = CredentialMaterial(
        api_key=SecretStr("key"),
        api_secret=SecretStr("secret"),
        passphrase=SecretStr("passphrase"),
        proxy_url=None,
    )

    with pytest.raises(BetaSourceUnavailable, match="final beta source unavailable"):
        manager.preview_bound_strategy("ins-1", strategy, Decimal("1250"), material, session_id="session-bound")

    assert journal.list_for_instance("ins-1") == []
    assert gateway.closed is True
    manager.close()


def test_stale_planned_bound_strategy_preview_is_invalidated_without_exchange_action(tmp_path) -> None:
    journal = InMemoryCampaignJournal()
    manager = CampaignWorkerManager(
        live_settings(tmp_path),
        EphemeralCredentialVault(),
        journal,
        lambda: FakeBetaProvider(sample_campaign().allocation),  # type: ignore[arg-type]
    )
    profile = live_profile(tmp_path)
    gateway = FakeGateway()
    manager._profile_and_gateway = lambda _material: (profile, gateway)  # type: ignore[method-assign]
    material = CredentialMaterial(
        api_key=SecretStr("key"),
        api_secret=SecretStr("secret"),
        passphrase=SecretStr("passphrase"),
        proxy_url=None,
    )
    initial = VolumeStrategy(
        id="strategy-bound",
        name="Initial",
        target_volume_quote=Decimal("5000"),
        round_turnover_quote_min=Decimal("220"),
        round_turnover_quote_max=Decimal("480"),
        position_hold_min_seconds=7,
        position_hold_max_seconds=9,
        round_interval_min_seconds=11,
        round_interval_max_seconds=13,
    )
    stale = manager.preview_bound_strategy("ins-1", initial, Decimal("1250"), material, session_id="session-1")
    updated = initial.model_copy(
        update={"name": "Updated", "version": 2, "round_turnover_quote_min": Decimal("221")}, deep=True
    )

    assert manager.invalidate_stale_planned_bound_strategy_previews(
        {"ins-1": updated}, reason="executor_startup_strategy_snapshot_stale"
    ) == ["ins-1"]
    record = journal.get(stale.campaign_id)
    assert record is not None
    assert record.status == BetaCampaignStatus.STOPPED.value
    assert record.metadata["invalidation_reason"] == "executor_startup_strategy_snapshot_stale"
    assert record.events[-1]["name"] == "bound_strategy_preview_invalidated"

    current = manager.preview_bound_strategy("ins-1", updated, Decimal("1250"), material, session_id="session-2")
    assert current.campaign_id != stale.campaign_id
    assert current.strategy_version == 2
    manager.close()
