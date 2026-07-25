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

from ..support.test_api_support import (
    LivePreviewGateway,
    LivePreviewProvider,
    UnavailableLivePreviewProvider,
    create_payload,
    strategy_payload,
)


def test_bound_strategy_preview_archives_incomplete_recovery_without_blocking_restart(
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
        first = api.post(f"/api/v1/instances/{instance['id']}/strategy-executions/preview", json={}).json()
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
        )
        app.state.session_volume.mark_recovering(
            session_id,
            reason="control_plane_restart",
            finished_at_ms=started_at_ms + 1_000,
        )
        app.state.campaign_journal.update(first["campaignId"], status="recovering")
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

        async def incomplete_fills(_instance_id: str, _start_ms: int, _end_ms: int):
            return (), False, "page_budget_exhausted"

        app.state.account_runtime.authoritative_session_fills = incomplete_fills
        reopened = api.post(f"/api/v1/instances/{instance['id']}/strategy-run/prepare", json={})

        assert reopened.status_code == 200
        assert reopened.json()["disposition"] == "ready"
        assert reopened.json()["preview"]["campaignId"] != first["campaignId"]
        session = app.state.trade_volume_ledger.get_session(session_id)
        assert session is not None
        assert session.status == "stopped"
        assert session.audit_status == "pending"
        assert app.state.trade_volume_ledger.active_session(instance["id"], "live") is None
        assert app.state.campaign_journal.get(first["campaignId"]).status == "stopped"


def test_bound_strategy_preview_returns_503_when_final_beta_source_is_unavailable(
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
    monkeypatch.setattr(main_module, "LiveCampaignBetaAllocationProvider", UnavailableLivePreviewProvider)
    monkeypatch.setattr(
        campaigns_module.CampaignWorkerManager,
        "_profile_and_gateway",
        lambda _self, _material: (profile, LivePreviewGateway()),
    )

    with TestClient(create_app(settings)) as api:
        strategy = api.post("/api/v1/strategies", json=strategy_payload(target="1250")).json()
        payload = create_payload(mode="live")
        payload["strategyId"] = strategy["id"]
        instance = api.post("/api/v1/instances", json=payload).json()
        preview = api.post(f"/api/v1/instances/{instance['id']}/strategy-executions/preview", json={})

    assert preview.status_code == 503
    assert preview.json()["detail"] == "final beta source unavailable: beta_request_failed:httperror"


def test_reassigning_a_shared_strategy_invalidates_old_planned_preview(
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
    with TestClient(create_app(settings)) as api:
        first = api.post("/api/v1/strategies", json=strategy_payload(name="First", target="1250")).json()
        second = api.post("/api/v1/strategies", json=strategy_payload(name="Second", target="1250")).json()
        payload = create_payload(mode="live")
        payload["strategyId"] = first["id"]
        instance = api.post("/api/v1/instances", json=payload).json()
        old_preview = api.post(f"/api/v1/instances/{instance['id']}/strategy-executions/preview", json={}).json()

        assigned = api.post(
            f"/api/v1/strategies/{second['id']}/assign",
            json={"instanceIds": [instance["id"]]},
        )
        assert assigned.status_code == 200, assigned.text
        old = api.get(f"/api/v1/instances/{instance['id']}/strategy-executions/{old_preview['campaignId']}").json()
        assert old["status"] == "stopped"
        assert old["reason"] == "strategy_binding_changed"

        rebound = api.post(
            f"/api/v1/strategies/{first['id']}/assign",
            json={"instanceIds": [instance["id"]]},
        )
        assert rebound.status_code == 200, rebound.text
        projection = api.get(f"/api/v1/instances/{instance['id']}").json()
        assert projection["strategyId"] == first["id"]
        assert projection["strategy"]["version"] == 1
        current_preview = api.post(f"/api/v1/instances/{instance['id']}/strategy-executions/preview", json={})
        assert current_preview.status_code == 200, current_preview.text
        assert current_preview.json()["campaignId"] != old_preview["campaignId"]
        assert current_preview.json()["strategyId"] == first["id"]


def test_shared_strategy_update_is_rejected_while_bound_execution_is_active(
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
        strategy = api.post("/api/v1/strategies", json=strategy_payload(target="1250")).json()
        payload = create_payload(mode="live")
        payload["strategyId"] = strategy["id"]
        instance = api.post("/api/v1/instances", json=payload).json()
        preview = api.post(f"/api/v1/instances/{instance['id']}/strategy-executions/preview", json={}).json()
        assert app.state.campaign_journal.claim_execution(preview["campaignId"], started_at_ms=1) is True

        changed = api.patch(
            f"/api/v1/strategies/{strategy['id']}",
            json=strategy_payload(name="must not apply", target="1250"),
        )
        assert changed.status_code == 409
        assert "active" in changed.json()["detail"]
        assert api.get("/api/v1/strategies").json()[0]["name"] != "must not apply"
