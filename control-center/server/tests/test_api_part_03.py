import time

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import SecretStr

import fleet_api.campaigns as campaigns_module
import fleet_api.main as main_module
from fleet_api.config import ControlPlaneSettings
from fleet_api.main import create_app
from fleet_api.models import (
    ExposureSnapshot,
    InstanceStatus,
    StrategyTargetMode,
)

from .test_api_support import (
    HeldWorkerExecutor,
    LivePreviewGateway,
    LivePreviewProvider,
    RefreshTrackingBetaProvider,
    client,
    create_payload,
    strategy_payload,
)


def test_bound_strategy_execution_creates_session_only_after_confirmed_idempotent_claim(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from weex_cli.config import Credentials, Settings
    from weex_cli.live_profile import LiveProfile

    settings = ControlPlaneSettings(
        adapter="weex-live",
        storage="sqlite",
        sqlite_path=tmp_path / "fleet.sqlite3",
        master_key=SecretStr(Fernet.generate_key().decode("ascii")),
        seed_demo_data=False,
        live_campaigns_enabled=True,
        live_trading_enabled=True,
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
    app = create_app(settings, require_command_id=True)
    held_executor = HeldWorkerExecutor()
    app.state.campaign_manager._executor = held_executor
    with TestClient(app) as api:
        strategy = api.post(
            "/api/v1/strategies",
            json=strategy_payload(target="1250"),
            headers={"X-Fleet-Command-Id": "strategy-create"},
        ).json()
        payload = create_payload(mode="live")
        payload["strategyId"] = strategy["id"]
        instance = api.post(
            "/api/v1/instances",
            json=payload,
            headers={"X-Fleet-Command-Id": "account-create"},
        ).json()
        preview = api.post(
            f"/api/v1/instances/{instance['id']}/strategy-executions/preview",
            json={},
            headers={"X-Fleet-Command-Id": "bound-preview"},
        ).json()
        assert app.state.trade_volume_ledger.latest_session(instance["id"], "live") is None
        assert api.get("/api/v1/health").json()["liveCampaignActiveWorkerCount"] == 0

        headers = {"X-Fleet-Command-Id": "bound-execute-once"}
        execution = api.post(
            f"/api/v1/instances/{instance['id']}/strategy-executions/{preview['campaignId']}/execute",
            json={"riskAcknowledged": True, "confirmation": preview["confirmation"]},
            headers=headers,
        )
        assert execution.status_code == 200, execution.text
        session = app.state.trade_volume_ledger.latest_session(instance["id"], "live")
        assert session is not None
        assert session["target_quote_volume"] == "1250"
        assert held_executor.submissions == 1
        assert api.get("/api/v1/health").json()["liveCampaignActiveWorkerCount"] == 1

        duplicate = api.post(
            f"/api/v1/instances/{instance['id']}/strategy-executions/{preview['campaignId']}/execute",
            json={"riskAcknowledged": True, "confirmation": preview["confirmation"]},
            headers=headers,
        )
        assert duplicate.status_code == 409
        assert held_executor.submissions == 1


def test_strategy_target_mode_and_funding_preflight_are_exposed_and_enforced() -> None:
    with client() as api:
        payload = strategy_payload(name="Impossible round", target="200000")
        payload.update(
            {
                "targetMode": "lifetime",
                "roundTurnoverQuoteMin": "200000",
                "roundTurnoverQuoteMax": "200000",
            }
        )
        strategy_response = api.post("/api/v1/strategies", json=payload)
        assert strategy_response.status_code == 201
        strategy = strategy_response.json()
        account_payload = create_payload()
        account_payload["strategyId"] = strategy["id"]
        created = api.post("/api/v1/instances", json=account_payload)
        assert created.status_code == 201
        snapshot = created.json()

        assert strategy["targetMode"] == "lifetime"
        assert snapshot["fundingPreflight"]["status"] == "insufficient"
        assert snapshot["fundingPreflight"]["requiredLeverage"] > 99
        rejected = api.post(f"/api/v1/instances/{snapshot['id']}/actions/start")

    assert rejected.status_code == 409
    assert "funding preflight failed" in rejected.json()["detail"]


def test_lifetime_target_requires_complete_trade_history_before_legacy_mock_start() -> None:
    app = create_app(ControlPlaneSettings(seed_demo_data=False))
    with TestClient(app) as api:
        created = api.post("/api/v1/instances", json=create_payload()).json()
        instance = app.state.fleet_repository.get(created["id"])
        app.state.fleet_repository.replace(
            instance.model_copy(
                update={
                    "strategy": instance.strategy.model_copy(update={"target_mode": StrategyTargetMode.LIFETIME}),
                    "volume": instance.volume.model_copy(update={"complete": False}),
                },
                deep=True,
            )
        )
        rejected = api.post(f"/api/v1/instances/{created['id']}/actions/start")

    assert rejected.status_code == 409
    assert "complete lifetime trade history" in rejected.json()["detail"]


def test_health_proves_live_trading_is_disabled() -> None:
    response = client().get("/api/v1/health")
    assert response.status_code == 200
    payload = response.json()
    assert {
        key: payload[key]
        for key in (
            "status",
            "adapter",
            "storage",
            "liveTradingEnabled",
            "executionEnabled",
            "liveCampaignsEnabled",
            "liveCampaignWorkerCount",
        )
    } == {
        "status": "ok",
        "adapter": "mock",
        "storage": "memory",
        "liveTradingEnabled": False,
        "executionEnabled": True,
        "liveCampaignsEnabled": False,
        "liveCampaignWorkerCount": 0,
    }
    assert payload["apiReleaseId"] == "dev"
    assert payload["executorConnected"] is True
    assert isinstance(payload["executorGeneration"], str)


def test_lifespan_runs_one_central_beta_refresher_at_the_configured_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = RefreshTrackingBetaProvider()

    def provider_factory(*_args, **kwargs):
        assert kwargs["cache_seconds"] == 0.01
        assert kwargs["network_on_demand"] is False
        return provider

    monkeypatch.setattr(main_module, "HttpBetaAllocationProvider", provider_factory)
    app = main_module.create_app(
        ControlPlaneSettings(
            seed_demo_data=False,
            mock_tick_interval_seconds=60,
            beta_refresh_interval_seconds=0.01,
            beta_background_refresh_enabled=True,
        )
    )

    with TestClient(app):
        time.sleep(0.035)

    assert provider.refresh_calls >= 3
    assert provider.closed is True


def test_readonly_adapter_exposes_health_but_rejects_execution_actions() -> None:
    app = create_app(ControlPlaneSettings(adapter="weex-readonly", seed_demo_data=False))
    with TestClient(app) as api:
        payload = api.get("/api/v1/health").json()
        assert {
            key: payload[key]
            for key in (
                "status",
                "adapter",
                "storage",
                "liveTradingEnabled",
                "executionEnabled",
                "liveCampaignsEnabled",
                "liveCampaignWorkerCount",
            )
        } == {
            "status": "ok",
            "adapter": "weex-readonly",
            "storage": "memory",
            "liveTradingEnabled": False,
            "executionEnabled": False,
            "liveCampaignsEnabled": False,
            "liveCampaignWorkerCount": 0,
        }
        assert payload["executorConnected"] is True
        assert isinstance(payload["executorGeneration"], str)
        created = api.post("/api/v1/instances", json=create_payload(mode="live"))
        assert created.status_code == 201
        assert created.json()["mockCycleTotalQuote"] is None
        instance_id = created.json()["id"]
        start = api.post(f"/api/v1/instances/{instance_id}/actions/start")
        stop = api.post(f"/api/v1/instances/{instance_id}/actions/stop")

    assert start.status_code == 409
    assert "read-only" in start.json()["detail"]
    assert stop.status_code == 200
    assert stop.json()["status"] == "stopped"


def test_mock_close_positions_endpoint_flattens_only_a_non_running_exposed_instance() -> None:
    app = create_app(ControlPlaneSettings(seed_demo_data=True))
    with TestClient(app) as api:
        before = api.get("/api/v1/instances/ins-api-02").json()
        response = api.post("/api/v1/instances/ins-api-02/positions/close")

    assert response.status_code == 200
    closed = response.json()
    assert closed["status"] == "warning"
    assert closed["exposure"] == {"btcLong": 0.0, "ethShort": 0.0}
    assert closed["volume"]["lifetime"] == pytest.approx(before["volume"]["lifetime"] + 938.1)
    assert closed["volume"]["today"] == pytest.approx(before["volume"]["today"] + 938.1)
    assert closed["strategyProgress"]["generatedVolumeQuote"] == "3738.1"
    assert "一键平仓完成" in closed["phase"]


def test_close_positions_endpoint_rejects_running_and_flat_instances() -> None:
    app = create_app(ControlPlaneSettings(seed_demo_data=True))
    with TestClient(app) as api:
        running = api.post("/api/v1/instances/ins-api-01/positions/close")
        created = api.post("/api/v1/instances", json=create_payload()).json()
        flat = api.post(f"/api/v1/instances/{created['id']}/positions/close")

    assert running.status_code == 409
    assert "stop or pause" in running.json()["detail"]
    assert flat.status_code == 409
    assert "no open positions" in flat.json()["detail"]


def test_readonly_adapter_rejects_position_close_even_when_exposure_exists() -> None:
    app = create_app(ControlPlaneSettings(adapter="weex-readonly", seed_demo_data=False))
    with TestClient(app) as api:
        created = api.post("/api/v1/instances", json=create_payload(mode="live")).json()
        instance = app.state.fleet_repository.get(created["id"])
        assert instance is not None
        app.state.fleet_repository.replace(
            instance.model_copy(
                update={
                    "status": InstanceStatus.STOPPED,
                    "exposure": ExposureSnapshot(btc_long=100, eth_short=44),
                },
                deep=True,
            )
        )
        response = api.post(f"/api/v1/instances/{created['id']}/positions/close")

    assert response.status_code == 409
    assert "read-only" in response.json()["detail"]
    unchanged = app.state.fleet_repository.get(created["id"])
    assert unchanged is not None
    assert unchanged.exposure == ExposureSnapshot(btc_long=100, eth_short=44)
