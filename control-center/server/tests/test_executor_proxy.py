from __future__ import annotations

import stat
import tempfile
from pathlib import Path

import httpx
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import SecretStr

from fleet_api.api_proxy import create_app as create_proxy_app
from fleet_api.config import ControlPlaneSettings
from fleet_api.executor_main import bind_executor_socket
from fleet_api.main import create_app


class AsyncSSEStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b'id: generation:execution:12\nevent: delta\ndata: {"type":"delta"}\n\n'


def test_executor_command_id_is_accepted_only_once() -> None:
    app = create_app(ControlPlaneSettings(seed_demo_data=False))
    payload = {
        "name": "One command",
        "targetVolumeQuote": "100",
        "roundTurnoverQuoteMin": "10",
        "roundTurnoverQuoteMax": "10",
        "positionHoldMinSeconds": 1,
        "positionHoldMaxSeconds": 1,
        "roundIntervalMinSeconds": 1,
        "roundIntervalMaxSeconds": 1,
    }
    with TestClient(app) as client:
        headers = {"X-Fleet-Command-Id": "test-command-001"}
        before = len(client.get("/api/v1/strategies").json())
        assert client.post("/api/v1/strategies", json=payload, headers=headers).status_code == 201
        repeated = client.post("/api/v1/strategies", json=payload, headers=headers)
        assert repeated.status_code == 409
        assert len(client.get("/api/v1/strategies").json()) == before + 1


def test_executor_requires_a_command_id_for_mutations() -> None:
    app = create_app(ControlPlaneSettings(seed_demo_data=False), require_command_id=True)
    with TestClient(app) as client:
        response = client.post("/api/v1/strategies", json={"name": "not-executed"})
    assert response.status_code == 400
    assert response.json()["detail"] == "X-Fleet-Command-Id is required"


def test_command_receipt_survives_an_executor_restart(tmp_path: Path) -> None:
    settings = ControlPlaneSettings(
        storage="sqlite",
        sqlite_path=tmp_path / "fleet.db",
        master_key=SecretStr(Fernet.generate_key().decode("ascii")),
        seed_demo_data=False,
    )
    payload = {
        "name": "Durable command",
        "targetVolumeQuote": "100",
        "roundTurnoverQuoteMin": "10",
        "roundTurnoverQuoteMax": "10",
        "positionHoldMinSeconds": 1,
        "positionHoldMaxSeconds": 1,
        "roundIntervalMinSeconds": 1,
        "roundIntervalMaxSeconds": 1,
    }
    headers = {"X-Fleet-Command-Id": "durable-command-001"}
    with TestClient(create_app(settings, require_command_id=True)) as client:
        assert client.post("/api/v1/strategies", json=payload, headers=headers).status_code == 201

    with TestClient(create_app(settings, require_command_id=True)) as restarted:
        status_response = restarted.get("/api/v1/commands/durable-command-001")
        assert status_response.json() == {"commandId": "durable-command-001", "status": "completed"}
        assert restarted.post("/api/v1/strategies", json=payload, headers=headers).status_code == 409


def test_executor_socket_is_owner_only() -> None:
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="wfe-") as directory:
        socket_path = Path(directory) / "executor.sock"
        listener = bind_executor_socket(socket_path)
        try:
            assert stat.S_IMODE(socket_path.stat().st_mode) == 0o600
            assert stat.S_IMODE(socket_path.parent.stat().st_mode) == 0o700
        finally:
            listener.close()
            socket_path.unlink()


def test_proxy_health_and_mutation_forward_through_executor_transport() -> None:
    calls: list[httpx.Request] = []

    def executor(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/_internal/executor-health":
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "adapter": "weex-live",
                    "storage": "sqlite",
                    "liveTradingEnabled": True,
                    "executionEnabled": False,
                    "liveCampaignsEnabled": True,
                    "liveCampaignActiveWorkerCount": 0,
                    "liveCampaignWorkerCount": 1,
                    "executorConnected": True,
                    "executorGeneration": "executor-test-generation",
                },
            )
        return httpx.Response(201, json={"id": "strategy-001"})

    app = create_proxy_app(
        Path("/tmp/fleet-executor.sock"),
        transport=httpx.MockTransport(executor),
        api_release_id="api-test-release",
        auth_required=False,
    )
    with TestClient(app) as client:
        health = client.get("/api/v1/health")
        assert health.status_code == 200
        assert health.json()["apiReleaseId"] == "api-test-release"
        assert health.json()["executorGeneration"] == "executor-test-generation"
        assert health.json()["liveCampaignActiveWorkerCount"] == 0
        response = client.post(
            "/api/v1/strategies",
            json={"name": "safe"},
            headers={"X-Fleet-Command-Id": "proxy-command-001"},
        )
        assert response.status_code == 201

    assert calls[-1].headers["x-fleet-command-id"] == "proxy-command-001"


def test_proxy_does_not_forward_a_mutation_without_command_id() -> None:
    calls: list[httpx.Request] = []

    def executor(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(201, json={"id": "should-not-run"})

    app = create_proxy_app(
        Path("/tmp/fleet-executor.sock"), transport=httpx.MockTransport(executor), auth_required=False
    )
    with TestClient(app) as client:
        response = client.post("/api/v1/strategies", json={"name": "not-executed"})
    assert response.status_code == 400
    assert calls == []


def test_proxy_health_is_degraded_when_executor_is_unavailable() -> None:
    def unavailable(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("socket unavailable")

    app = create_proxy_app(
        Path("/tmp/fleet-executor.sock"), transport=httpx.MockTransport(unavailable), auth_required=False
    )
    with TestClient(app) as client:
        response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json()["status"] == "degraded"
    assert response.json()["executorConnected"] is False


def test_proxy_never_retries_a_failed_mutation() -> None:
    calls = 0

    def unavailable(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("socket unavailable")

    app = create_proxy_app(
        Path("/tmp/fleet-executor.sock"), transport=httpx.MockTransport(unavailable), auth_required=False
    )
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/strategies",
            json={"name": "not-retried"},
            headers={"X-Fleet-Command-Id": "failed-once"},
        )
    assert response.status_code == 503
    assert response.json()["detail"] == "executor unavailable; no command was retried"
    assert calls == 1


def test_proxy_reports_command_acknowledgement_timeout_without_retrying() -> None:
    calls = 0

    def delayed_executor(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("slow local preflight")

    app = create_proxy_app(
        Path("/tmp/fleet-executor.sock"), transport=httpx.MockTransport(delayed_executor), auth_required=False
    )
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/strategies",
            json={"name": "not-retried-after-timeout"},
            headers={"X-Fleet-Command-Id": "timed-out-once"},
        )

    assert response.status_code == 504
    assert response.json()["commandId"] == "timed-out-once"
    assert "no command was retried" in response.json()["detail"]
    assert calls == 1


def test_proxy_streams_monitor_sse_and_forwards_resume_cursor() -> None:
    calls: list[httpx.Request] = []

    def executor(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=AsyncSSEStream(),
        )

    app = create_proxy_app(
        Path("/tmp/fleet-executor.sock"), transport=httpx.MockTransport(executor), auth_required=False
    )
    with TestClient(app) as client:
        response = client.get(
            "/api/v1/instances/ins-1/strategy-monitor/events?sessionId=session-1",
            headers={"Last-Event-ID": "generation:execution:11"},
        )

    assert response.status_code == 200
    assert "event: delta" in response.text
    assert len(calls) == 1
    assert calls[0].url.params["sessionId"] == "session-1"
    assert calls[0].headers["last-event-id"] == "generation:execution:11"
    assert calls[0].extensions["timeout"]["read"] is None


def test_primary_frontend_flow_uses_bound_strategy_execution_not_legacy_campaign_dialog() -> None:
    app_source = (Path(__file__).resolve().parents[2] / "src" / "App.tsx").read_text(encoding="utf-8")

    assert "BoundStrategyExecutionDialog" in app_source
    assert "BetaCampaignDialog" not in app_source
    assert "beta-campaigns" not in app_source
