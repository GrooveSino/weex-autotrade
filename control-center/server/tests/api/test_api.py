import time
from decimal import Decimal

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import SecretStr

import fleet_api.campaigns as campaigns_module
import fleet_api.main as main_module
from fleet_api.config.config import ControlPlaneSettings
from fleet_api.main import create_app
from fleet_api.volume.core.volume_history import NormalizedTradeFill

from ..support.test_api_support import (
    LivePreviewGateway,
    LivePreviewProvider,
    client,
    create_payload,
    strategy_payload,
)


def test_strategy_monitor_is_idle_without_a_run_and_never_exposes_credentials() -> None:
    with client() as api:
        instance = api.post("/api/v1/instances", json=create_payload()).json()
        response = api.get(f"/api/v1/instances/{instance['id']}/strategy-monitor")

    assert response.status_code == 200
    payload = response.json()
    assert payload["instanceId"] == instance["id"]
    assert payload["status"] == "idle"
    assert payload["timeline"] == []
    assert "secret-never-return" not in response.text
    assert "pass-never-return" not in response.text
    assert "proxy-password" not in response.text


def test_beta_source_settings_update_runtime_without_storing_endpoint_credentials() -> None:
    class NoNetworkProvider:
        last_refresh_error = None

        async def refresh(self) -> bool:
            return True

        def seconds_until_refresh(self, maximum_seconds: float) -> float:
            return maximum_seconds

        async def aclose(self) -> None:
            return None

    app = create_app(ControlPlaneSettings(seed_demo_data=False))
    runtime = app.state.beta_source_runtime
    runtime._provider = NoNetworkProvider()  # type: ignore[attr-defined]
    runtime._provider_factory = lambda _settings: NoNetworkProvider()  # type: ignore[attr-defined]
    with TestClient(app) as api:
        current = api.get("/api/v1/beta/source")
        assert current.status_code == 200
        assert current.json()["url"] == "http://127.0.0.1:5888/api/v1/hedge-ratio"

        rejected = api.patch(
            "/api/v1/beta/source",
            json={
                "url": "https://user:password@beta.example.test/ratio",
                "timeoutSeconds": 2,
                "refreshIntervalSeconds": 5,
                "backgroundRefreshEnabled": True,
            },
        )
        assert rejected.status_code == 422

        updated = api.patch(
            "/api/v1/beta/source",
            json={
                "url": "https://beta.example.test/api/v1/ratio",
                "timeoutSeconds": 2.5,
                "refreshIntervalSeconds": 5,
                "backgroundRefreshEnabled": True,
            },
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["url"] == "https://beta.example.test/api/v1/ratio"
        assert updated.json()["timeoutSeconds"] == 2.5
        assert api.get("/api/v1/beta/source").json()["url"] == "https://beta.example.test/api/v1/ratio"


def test_bound_strategy_live_preview_is_read_only_and_confirmation_gated(
    tmp_path, monkeypatch: pytest.MonkeyPatch
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
    app = create_app(settings)
    with TestClient(app) as api:
        health = api.get("/api/v1/health").json()
        assert health["boundStrategyExecutionEnabled"] is True
        strategy = api.post("/api/v1/strategies", json=strategy_payload(target="1250")).json()
        payload = create_payload(mode="live")
        payload["strategyId"] = strategy["id"]
        created = api.post("/api/v1/instances", json=payload)
        assert created.status_code == 201
        instance = created.json()
        preview = api.post(f"/api/v1/instances/{instance['id']}/strategy-executions/preview", json={})
        assert preview.status_code == 200, preview.text
        body = preview.json()
        assert body["strategyId"] == strategy["id"]
        assert body["strategyVersion"] == 1
        assert body["targetMode"] == "incremental"
        assert body["runDisposition"] == "new_incremental"
        assert body["strategyTargetQuoteVolume"] == "1250"
        assert body["executionTargetQuoteVolume"] == "1250"
        assert body["roundTurnoverQuoteMin"] == "500"
        assert body["cycleVolume"] == "750"
        assert "STRATEGY" in body["confirmation"]
        changed = strategy_payload(name="Changed after preview", target="1250")
        updated = api.patch(f"/api/v1/strategies/{strategy['id']}", json=changed)
        assert updated.status_code == 200, updated.text
        assert updated.json()["version"] == 2
        projection = api.get(f"/api/v1/instances/{instance['id']}")
        assert projection.status_code == 200
        assert projection.json()["strategy"]["name"] == "Changed after preview"
        assert projection.json()["strategy"]["version"] == 2
        executions = api.get(f"/api/v1/instances/{instance['id']}/strategy-executions")
        assert executions.status_code == 200
        assert executions.json()[0]["status"] == "stopped"
        assert executions.json()[0]["reason"] == "shared_strategy_updated"
        invalidation_events = api.get(
            f"/api/v1/instances/{instance['id']}/strategy-executions/{body['campaignId']}/events"
        )
        assert invalidation_events.status_code == 200
        assert invalidation_events.json()[-1]["name"] == "bound_strategy_preview_invalidated"
        refreshed = api.post(f"/api/v1/instances/{instance['id']}/strategy-executions/preview", json={})
        assert refreshed.status_code == 200, refreshed.text
        assert refreshed.json()["campaignId"] != body["campaignId"]
        assert refreshed.json()["strategyVersion"] == 2
        assert refreshed.json()["strategyName"] == "Changed after preview"
        stale = api.post(
            f"/api/v1/instances/{instance['id']}/strategy-executions/{body['campaignId']}/execute",
            json={"riskAcknowledged": True, "confirmation": body["confirmation"]},
        )
        assert stale.status_code == 409
        assert "changed since preview" in stale.json()["detail"]
        assert (
            api.post(
                f"/api/v1/instances/{instance['id']}/strategy-executions/{body['campaignId']}/execute",
                json={"riskAcknowledged": False, "confirmation": body["confirmation"]},
            ).status_code
            == 409
        )
        assert api.get("/api/v1/health").json()["liveCampaignActiveWorkerCount"] == 0


def test_bound_strategy_preview_automatically_converges_a_flat_uncertain_run(
    tmp_path, monkeypatch: pytest.MonkeyPatch
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
            credentials=Credentials("key", "secret", "pass"),
            default_mode="live",
            live_trading_enabled=True,
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
        first = api.post(
            f"/api/v1/instances/{instance['id']}/strategy-executions/preview",
            json={},
        ).json()
        record = app.state.campaign_journal.get(first["campaignId"])
        assert record is not None
        session_id = str(record.metadata["session_id"])
        started_at_ms = int(time.time() * 1000) - 2_000
        assert app.state.campaign_journal.claim_execution(first["campaignId"], started_at_ms=started_at_ms)
        app.state.session_volume.start(
            session_id=session_id,
            account_id=instance["id"],
            mode="live",
            started_at_ms=started_at_ms,
            target_quote_volume=Decimal("1250"),
            strategy_id=strategy["id"],
            strategy_name=strategy["name"],
            strategy_version=strategy["version"],
            target_mode="incremental",
            strategy_target_quote_volume=Decimal("1250"),
        )
        app.state.session_volume.mark_recovering(
            session_id,
            reason="control_plane_restart",
            finished_at_ms=started_at_ms + 1_000,
        )
        app.state.campaign_journal.update(
            first["campaignId"],
            status="recovering",
            reason="control_plane_restart",
        )
        app.state.campaign_manager._append_monitor_event(
            record,
            {
                "sequence": 1,
                "name": "leg_progress",
                "at_ms": started_at_ms + 100,
                "fields": {"progress_event": "order_submission_attempted"},
                "message": "leg progress",
            },
        )
        authoritative = (
            NormalizedTradeFill(
                identity="recovered-fill",
                executed_at_ms=started_at_ms + 500,
                quote_volume=Decimal("12.5"),
                symbol="BTCUSDT",
                position_action="open",
                maker=True,
            ),
        )

        async def authoritative_fills(_instance_id: str, _start_ms: int, _end_ms: int):
            return authoritative, True, "window_complete"

        app.state.account_runtime.authoritative_session_fills = authoritative_fills
        second = api.post(
            f"/api/v1/instances/{instance['id']}/strategy-executions/preview",
            json={},
        )

        assert second.status_code == 200, second.text
        assert second.json()["campaignId"] != first["campaignId"]
        assert second.json()["executionTargetQuoteVolume"] == "1250"
        recovered = app.state.trade_volume_ledger.session_projection(session_id)
        assert recovered["status"] == "stopped"
        assert recovered["verified_quote_volume"] == "12.5"
        assert recovered["result_reason"] == "automatic_startup_recovery"
        archived = app.state.campaign_journal.get(first["campaignId"])
        assert archived is not None
        assert archived.status == "stopped"
        assert archived.metadata["reconciliation_source"] == "automatic_startup_recovery"
