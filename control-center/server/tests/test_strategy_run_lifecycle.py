import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import SecretStr
from weex_cli.config import Credentials, Settings
from weex_cli.live_profile import LiveProfile

import fleet_api.campaigns as campaigns_module
import fleet_api.main as main_module
from fleet_api.campaign_helpers import _cleanup_confirmation
from fleet_api.campaigns import CampaignWorkerManager, InMemoryCampaignJournal
from fleet_api.config import ControlPlaneSettings
from fleet_api.main import create_app
from fleet_api.models import BetaCampaignStatus
from fleet_api.service import UnsafeOperation
from fleet_api.vault import CredentialMaterial, EphemeralCredentialVault

from .test_api_support import LivePreviewGateway, LivePreviewProvider, create_payload, strategy_payload
from .test_campaigns_support import FakeBetaProvider, FakeGateway, live_settings, metadata, sample_campaign


def _profile(tmp_path) -> LiveProfile:
    return LiveProfile(
        path=tmp_path / "profile.toml",
        settings=Settings(
            credentials=Credentials("key", "secret", "pass"),
            default_mode="live",
            live_trading_enabled=True,
        ),
        proxy_url=None,
        allow_live_mutations=True,
        post_only_only=True,
    )


def _live_settings(tmp_path) -> ControlPlaneSettings:
    return ControlPlaneSettings(
        adapter="weex-live",
        storage="sqlite",
        sqlite_path=tmp_path / "fleet.sqlite3",
        master_key=SecretStr(Fernet.generate_key().decode("ascii")),
        seed_demo_data=False,
        live_campaigns_enabled=True,
        live_trading_enabled=True,
        campaign_data_directory=tmp_path / "campaigns",
    )


def test_reopening_planned_preview_rechecks_boundary_and_offers_cleanup(tmp_path, monkeypatch) -> None:
    profile = _profile(tmp_path)
    monkeypatch.setattr(main_module, "LiveCampaignBetaAllocationProvider", LivePreviewProvider)
    monkeypatch.setattr(
        campaigns_module.CampaignWorkerManager,
        "_profile_and_gateway",
        lambda _self, _material: (profile, LivePreviewGateway()),
    )
    app = create_app(_live_settings(tmp_path))
    with TestClient(app) as api:
        strategy = api.post("/api/v1/strategies", json=strategy_payload(target="500")).json()
        payload = create_payload(mode="live")
        payload["strategyId"] = strategy["id"]
        instance = api.post("/api/v1/instances", json=payload).json()
        first = api.post(f"/api/v1/instances/{instance['id']}/strategy-run/prepare", json={}).json()
        campaign_id = first["preview"]["campaignId"]
        app.state.campaign_manager.inspect_bound_strategy_boundary = lambda _material: {
            "flat": False,
            "position_count": 1,
            "regular_order_count": 2,
            "trigger_order_count": 1,
            "available_quote": "1000",
        }

        second = api.post(f"/api/v1/instances/{instance['id']}/strategy-run/prepare", json={})

        assert second.status_code == 200
        body = second.json()
        assert body["disposition"] == "cleanup_required"
        assert (body["positionCount"], body["regularOrderCount"], body["triggerOrderCount"]) == (1, 2, 1)
        assert body["cleanupConfirmation"].startswith("CLEANUP WEEX LIVE STRATEGY ")
        archived = app.state.campaign_journal.get(campaign_id)
        assert archived is not None
        assert archived.status == BetaCampaignStatus.STOPPED.value
        assert archived.metadata["reason"] == "launch_preview_boundary_changed"


def test_prepare_reuses_sampled_target_until_direction_or_run_changes(tmp_path, monkeypatch) -> None:
    profile = _profile(tmp_path)
    monkeypatch.setattr(main_module, "LiveCampaignBetaAllocationProvider", LivePreviewProvider)
    monkeypatch.setattr(
        campaigns_module.CampaignWorkerManager,
        "_profile_and_gateway",
        lambda _self, _material: (profile, LivePreviewGateway()),
    )
    app = create_app(_live_settings(tmp_path))
    with TestClient(app) as api:
        strategy_request = strategy_payload(target="1500")
        strategy_request.update(
            {
                "targetVolumeQuoteMin": "1000.00",
                "targetVolumeQuoteMax": "1500.00",
            }
        )
        strategy = api.post("/api/v1/strategies", json=strategy_request).json()
        payload = create_payload(mode="live")
        payload["strategyId"] = strategy["id"]
        instance = api.post("/api/v1/instances", json=payload).json()
        path = f"/api/v1/instances/{instance['id']}/strategy-run/prepare"

        first = api.post(path, json={}).json()["preview"]
        repeated = api.post(path, json={}).json()["preview"]

        assert repeated["campaignId"] == first["campaignId"]
        assert repeated["selectedTargetQuoteVolume"] == first["selectedTargetQuoteVolume"]
        assert repeated["confirmation"] == first["confirmation"]

        reverse = api.post(path, json={"direction": "btc_short_eth_long"}).json()["preview"]
        assert reverse["campaignId"] != first["campaignId"]
        assert reverse["direction"] == "btc_short_eth_long"
        assert "DIRECTION_BTC_SHORT_ETH_LONG" in reverse["confirmation"]
        archived = app.state.campaign_journal.get(first["campaignId"])
        assert archived is not None
        assert archived.status == BetaCampaignStatus.STOPPED.value

        app.state.campaign_journal.update(
            reverse["campaignId"],
            status=BetaCampaignStatus.STOPPED.value,
            finished_at_ms=int(time.time() * 1000),
            reason="test_run_finished",
        )
        next_run = api.post(path, json={"direction": "btc_short_eth_long"}).json()["preview"]
        assert next_run["campaignId"] != reverse["campaignId"]
        assert 1000 <= float(next_run["selectedTargetQuoteVolume"]) <= 1500


def test_cleanup_rejects_concurrent_command_for_same_account(tmp_path, monkeypatch) -> None:
    manager = CampaignWorkerManager(
        live_settings(tmp_path),
        EphemeralCredentialVault(),
        InMemoryCampaignJournal(),
        lambda: FakeBetaProvider(sample_campaign().allocation),  # type: ignore[arg-type]
    )
    now_ms = int(time.time() * 1000)
    campaign = replace(
        sample_campaign(),
        created_at_ms=now_ms,
        expires_at_ms=now_ms + 3_600_000,
    )._with_computed_id()
    manager.journal.create("ins-1", campaign, metadata(campaign))
    manager.journal.update(campaign.campaign_id, status=BetaCampaignStatus.RECOVERING.value)
    material = CredentialMaterial(
        api_key=SecretStr("key"),
        api_secret=SecretStr("secret"),
        passphrase=SecretStr("passphrase"),
        proxy_url=None,
    )
    profile = _profile(tmp_path)
    manager._profile_and_gateway = lambda _material: (profile, FakeGateway())  # type: ignore[method-assign]
    entered = threading.Event()
    release = threading.Event()

    class BlockingCleanupService:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def cleanup(self, _plan):
            entered.set()
            assert release.wait(timeout=3)
            return {"status": "stopped", "reason": "cleanup_completed"}

    monkeypatch.setattr(campaigns_module, "LiveBetaVolumeCampaignService", BlockingCleanupService)
    confirmation = _cleanup_confirmation(campaign.campaign_id)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            manager.cleanup_bound_strategy,
            "ins-1",
            campaign.campaign_id,
            confirmation,
            material,
        )
        assert entered.wait(timeout=3)
        with pytest.raises(UnsafeOperation, match="already running"):
            manager.cleanup_bound_strategy("ins-1", campaign.campaign_id, confirmation, material)
        release.set()
        assert first.result(timeout=3)["verified"] is True
    manager.close()
