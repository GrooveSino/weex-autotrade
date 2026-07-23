import time

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import SecretStr

from fleet_api.config import ControlPlaneSettings
from fleet_api.main import create_app

from .test_api_support import create_payload, monitor_campaign
from .test_campaigns_support import sample_campaign


def _execution_event(*, remaining_quote: str, at_ms: int) -> dict[str, object]:
    return {
        "name": "campaign_run_started",
        "run": 1,
        "at_ms": at_ms,
        "fields": {"remaining_quote": remaining_quote},
    }


def test_clear_hides_existing_campaign_events_but_keeps_new_events_visible() -> None:
    app = create_app(ControlPlaneSettings(seed_demo_data=False, mock_tick_interval_seconds=60))
    with TestClient(app) as api:
        instance_id = api.post("/api/v1/instances", json=create_payload()).json()["id"]
        now_ms = int(time.time() * 1000)
        campaign = monitor_campaign(campaign_id="wv-log-clear-memory", created_at_ms=now_ms)
        app.state.campaign_journal.create(instance_id, campaign, {})
        app.state.campaign_journal.add_event(
            campaign.campaign_id,
            _execution_event(remaining_quote="500", at_ms=now_ms),
        )

        before = api.get(f"/api/v1/instances/{instance_id}/log-updates?limit=50").json()
        cleared = api.delete(f"/api/v1/instances/{instance_id}/log-updates")
        after = api.get(f"/api/v1/instances/{instance_id}/log-updates?limit=50").json()

        app.state.campaign_journal.add_event(
            campaign.campaign_id,
            _execution_event(remaining_quote="250", at_ms=now_ms + 1),
        )
        with_new_event = api.get(f"/api/v1/instances/{instance_id}/log-updates?limit=50").json()

    assert any("剩余目标 500" in line["message"] for line in before["lines"])
    assert cleared.status_code == 204
    assert after["lines"] == []
    assert [line["message"] for line in with_new_event["lines"]] == [
        "实盘执行：运行 1 开始；剩余目标 250 USDT"
    ]


def test_campaign_log_clear_boundary_survives_sqlite_restart(tmp_path) -> None:
    database = tmp_path / "fleet.db"
    key = SecretStr(Fernet.generate_key().decode("ascii"))
    settings = ControlPlaneSettings(
        storage="sqlite",
        sqlite_path=database,
        master_key=key,
        seed_demo_data=False,
        mock_tick_interval_seconds=60,
        campaign_data_directory=tmp_path / "campaigns",
    )
    first = create_app(settings)
    now_ms = int(time.time() * 1000)
    campaign = sample_campaign()
    with TestClient(first) as api:
        instance_id = api.post("/api/v1/instances", json=create_payload()).json()["id"]
        first.state.campaign_journal.create(instance_id, campaign, {})
        first.state.campaign_journal.add_event(
            campaign.campaign_id,
            _execution_event(remaining_quote="500", at_ms=now_ms),
        )
        assert api.delete(f"/api/v1/instances/{instance_id}/log-updates").status_code == 204

    second = create_app(settings)
    with TestClient(second) as api:
        assert api.get(f"/api/v1/instances/{instance_id}/log-updates?limit=50").json()["lines"] == []
        second.state.campaign_journal.add_event(
            campaign.campaign_id,
            _execution_event(remaining_quote="125", at_ms=now_ms + 1),
        )
        lines = api.get(f"/api/v1/instances/{instance_id}/log-updates?limit=50").json()["lines"]

    assert [line["message"] for line in lines] == ["实盘执行：运行 1 开始；剩余目标 125 USDT"]
