import time

from fastapi.testclient import TestClient

import fleet_api.campaigns as campaigns_module
import fleet_api.main as main_module
from fleet_api.campaign_events import _sanitize_event
from fleet_api.main import create_app

from .test_api_support import LivePreviewGateway, LivePreviewProvider, create_payload, strategy_payload
from .test_strategy_run_lifecycle import _live_settings, _profile


def test_flat_recovering_run_is_archived_and_can_prepare_again(tmp_path, monkeypatch) -> None:
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
        path = f"/api/v1/instances/{instance['id']}/strategy-run/prepare"
        first = api.post(path, json={}).json()["preview"]
        campaign_id = first["campaignId"]
        app.state.campaign_journal.add_event(
            campaign_id,
            _sanitize_event({"event": "order_submission_attempted", "symbol": "BTC", "action": "open"}),
        )
        app.state.campaign_journal.update(
            campaign_id,
            status="recovering",
            reason="worker_safety:position_quantity_invalid",
            finished_at_ms=int(time.time() * 1_000),
        )
        app.state.campaign_manager.inspect_bound_strategy_boundary = lambda _material: {
            "flat": True,
            "position_count": 0,
            "regular_order_count": 0,
            "trigger_order_count": 0,
            "available_quote": "1000",
            "blocking_positions": [],
            "checked_at_ms": int(time.time() * 1_000),
        }

        response = api.post(path, json={})

        assert response.status_code == 200
        assert response.json()["disposition"] == "ready"
        assert response.json()["preview"]["campaignId"] != campaign_id
        archived = app.state.campaign_journal.get(campaign_id)
        assert archived is not None
        assert archived.status == "stopped"
        assert archived.metadata["recovery_boundary_state"] == "flat"
