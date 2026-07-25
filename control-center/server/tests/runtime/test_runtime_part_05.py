from fastapi.testclient import TestClient

from fleet_api.config.config import ControlPlaneSettings
from fleet_api.main import create_app

from ..support.test_runtime_support import (
    MixedFactory,
    payload,
)


def test_manual_refresh_returns_sanitized_adapter_failure() -> None:
    factory = MixedFactory("pending")
    app = create_app(
        ControlPlaneSettings(seed_demo_data=False, mock_tick_interval_seconds=60),
        adapter_factory=factory,
    )
    with TestClient(app, raise_server_exceptions=False) as api:
        instance_id = api.post(
            "/api/v1/instances",
            json=payload("failing", "api-key-FAIL", "user:private@proxy.example.com:9401"),
        ).json()["id"]
        factory.failing_id = instance_id
        response = api.post(f"/api/v1/instances/{instance_id}/refresh")

    assert response.status_code == 503
    assert response.json()["detail"] == "telemetry unavailable (RuntimeError)"
    assert "api-key-FAIL" not in response.text
    assert "private" not in response.text
