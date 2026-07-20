import time
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import SecretStr
from weex_cli.beta_allocation import BetaAllocation
from weex_cli.beta_campaign import BetaVolumeCampaign, campaign_confirmation, live_profile_fingerprint
from weex_cli.config import Credentials, Settings
from weex_cli.live_profile import LiveProfile

from fleet_api.campaigns import CampaignWorkerManager, InMemoryCampaignJournal, SQLiteCampaignJournal
from fleet_api.config import ControlPlaneSettings
from fleet_api.models import BetaCampaignPreviewRequest, BetaCampaignStatus
from fleet_api.service import UnsafeOperation
from fleet_api.vault import CredentialMaterial, EphemeralCredentialVault


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


def test_journal_assigns_monotonic_event_sequences() -> None:
    journal = InMemoryCampaignJournal()
    campaign = sample_campaign()
    journal.create("ins-1", campaign, metadata(campaign))
    assert journal.add_event(campaign.campaign_id, {"sequence": 99, "name": "first"}) == 1
    assert journal.add_event(campaign.campaign_id, {"name": "second"}) == 2
    record = journal.get(campaign.campaign_id)
    assert record is not None
    assert [event["sequence"] for event in record.events] == [1, 2]


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
    manager = CampaignWorkerManager(
        live_settings(tmp_path),
        EphemeralCredentialVault(),
        InMemoryCampaignJournal(),
        lambda: FakeBetaProvider(allocation),  # type: ignore[arg-type]
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
    manager._verify_profile_fingerprint = lambda _record, _material: None  # type: ignore[method-assign]
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
    assert [event["sequence"] for event in record.events] == [1]
    lanes = captured["lanes"]
    assert isinstance(lanes, dict)
    assert lanes["BTC"] is not lanes["ETH"]
    assert lanes["BTC"] is not captured["primary"]
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
    manager._verify_profile_fingerprint = lambda _record, _material: None  # type: ignore[method-assign]
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
