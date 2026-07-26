from __future__ import annotations

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import SecretStr
from weex_cli.core.config import Credentials, Settings
from weex_cli.live_profile import LiveProfile

import fleet_api.campaigns as campaigns_module
import fleet_api.main as main_module
from fleet_api.config.config import ControlPlaneSettings
from fleet_api.main import create_app

from .support.test_api_support import LivePreviewGateway, LivePreviewProvider, create_payload, strategy_payload


def test_capacity_endpoint_reports_actor_io_process_and_sync_state() -> None:
    app = create_app(ControlPlaneSettings(seed_demo_data=False))
    with TestClient(app) as api:
        response = api.get("/api/v1/executor/capacity")

    assert response.status_code == 200
    body = response.json()
    assert body["maxActiveExecutions"] == 200
    assert body["maxNormalPhases"] == 20
    assert body["maxNormalIo"] == 64
    assert body["maxEmergencyIo"] == 32
    assert body["actorCount"] == 0
    assert body["openFileDescriptors"] > 0
    assert body["rssBytes"] > 0
    assert body["historySyncQueued"] == 0


def test_confirm_returns_capacity_full_without_starting_a_hidden_worker(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings = ControlPlaneSettings(
        adapter="weex-live",
        storage="sqlite",
        sqlite_path=tmp_path / "fleet.sqlite3",
        master_key=SecretStr(Fernet.generate_key().decode("ascii")),
        seed_demo_data=False,
        live_campaigns_enabled=True,
        live_trading_enabled=True,
        max_active_executions=1,
        campaign_data_directory=tmp_path / "campaigns",
    )
    profile = LiveProfile(
        path=tmp_path / "profile.toml",
        settings=Settings(
            credentials=Credentials("key", "secret", "pass"), default_mode="live", live_trading_enabled=True
        ),
        proxy_url=None,
        allow_live_mutations=True,
        post_only_only=True,
    )
    monkeypatch.setattr(main_module, "LiveCampaignBetaAllocationProvider", LivePreviewProvider)
    monkeypatch.setattr(
        campaigns_module.CampaignWorkerManager,
        "_profile_and_gateway",
        lambda _self, _material: (profile, LivePreviewGateway()),
    )
    app = create_app(settings)
    with TestClient(app) as api:
        strategy = api.post("/api/v1/strategies", json=strategy_payload(target="1250")).json()
        payload = create_payload(mode="live")
        payload["strategyId"] = strategy["id"]
        instance = api.post("/api/v1/instances", json=payload).json()
        preview = api.post(f"/api/v1/instances/{instance['id']}/strategy-executions/preview", json={}).json()
        assert app.state.campaign_manager.capacity.admit("occupied")
        response = api.post(
            f"/api/v1/instances/{instance['id']}/strategy-run/confirm",
            json={
                "executionId": preview["campaignId"],
                "confirmation": preview["confirmation"],
                "riskAcknowledged": True,
            },
        )
        app.state.campaign_manager.capacity.release_execution("occupied")

    assert response.status_code == 200, response.text
    assert response.json()["admissionState"] == "capacity_full"
    assert response.json()["capacity"]["activeExecutions"] == 1
