import time
from decimal import Decimal

from fastapi.testclient import TestClient

from fleet_api.config import ControlPlaneSettings
from fleet_api.execution import CycleExecutionStatus, PairCyclePlan
from fleet_api.main import create_app

from .test_api_support import (
    StaticBetaMarketProvider,
    client,
    create_payload,
    strategy_payload,
)


def test_runtime_metrics_start_empty_and_expose_the_configured_parallelism_cap() -> None:
    response = client().get("/api/v1/runtime/metrics")

    assert response.status_code == 200
    assert response.json() == {
        "maxParallelPolls": 12,
        "activePolls": 0,
        "maxObservedParallelism": 0,
        "pollRounds": 0,
        "accountsPolled": 0,
        "successfulPolls": 0,
        "failedPolls": 0,
        "lastRoundAccountCount": 0,
        "lastRoundSucceeded": 0,
        "lastRoundFailed": 0,
        "lastRoundStartedAtMs": None,
        "lastRoundCompletedAtMs": None,
        "lastRoundDurationMs": None,
    }

def test_beta_snapshot_endpoint_exposes_final_beta_without_filtering_upstream_usable() -> None:
    app = create_app(ControlPlaneSettings(seed_demo_data=False))
    with TestClient(app) as api:
        app.state.beta_market_provider = StaticBetaMarketProvider()
        response = api.get("/api/v1/beta")

    assert response.status_code == 200
    assert response.json()["finalBeta"] == "0.44260456370165036"
    assert response.json()["upstreamUsable"] is False
    assert response.json()["status"] == "low_confidence"
    assert response.json()["source"] == "beta_v2"

def test_create_returns_only_redacted_account_and_keeps_secrets_in_ephemeral_vault() -> None:
    app = create_app(ControlPlaneSettings(seed_demo_data=False))
    with TestClient(app) as api:
        response = api.post("/api/v1/instances", json=create_payload())

    assert response.status_code == 201
    body = response.json()
    serialized = response.text
    assert body["apiKeyTail"] == "ABCD"
    assert body["proxy"]["host"] == "proxy.example.com:9341"
    assert body["volume"]["complete"] is True
    assert "key-super-secret" not in serialized
    assert "secret-never-return" not in serialized
    assert "proxy-password" not in serialized
    assert len(app.state.credential_vault) == 1

def test_create_http_proxy_preserves_the_http_connection_scheme() -> None:
    app = create_app(ControlPlaneSettings(seed_demo_data=False))
    payload = create_payload()
    payload["proxy"] = {
        "type": "http",
        "url": "proxy-user:proxy-password@proxy.example.com:8080",
    }

    with TestClient(app) as api:
        response = api.post("/api/v1/instances", json=payload)

    assert response.status_code == 201
    instance_id = response.json()["id"]
    material = app.state.credential_vault.get(instance_id)
    assert material is not None
    assert material.proxy_url.get_secret_value() == "http://proxy-user:proxy-password@proxy.example.com:8080"

def test_create_without_proxy_keeps_direct_connection_out_of_the_vault() -> None:
    app = create_app(ControlPlaneSettings(seed_demo_data=False))
    payload = create_payload()
    payload["proxy"] = {"type": "none"}

    with TestClient(app) as api:
        response = api.post("/api/v1/instances", json=payload)

    assert response.status_code == 201
    instance_id = response.json()["id"]
    assert response.json()["proxy"]["type"] == "none"
    assert response.json()["proxy"]["host"] == "不使用代理"
    material = app.state.credential_vault.get(instance_id)
    assert material is not None
    assert material.proxy_url is None

def test_shared_strategy_is_created_once_and_updates_every_assigned_account_projection() -> None:
    app = create_app(ControlPlaneSettings(seed_demo_data=False))

    with TestClient(app) as api:
        created_strategy = api.post("/api/v1/strategies", json=strategy_payload())
        strategy_id = created_strategy.json()["id"]
        first_payload = create_payload()
        first_payload["strategyId"] = strategy_id
        first = api.post("/api/v1/instances", json=first_payload)
        second_payload = create_payload()
        second_payload.update({"name": "Test 02", "strategyId": strategy_id})
        second_payload["credentials"] = {
            "apiKey": "key-super-secret-EFGH",
            "apiSecret": "secret-two",
            "passphrase": "pass-two",
        }
        second = api.post("/api/v1/instances", json=second_payload)
        changed = strategy_payload(name="25k shared", target="25000")
        changed.update({"roundTurnoverQuoteMin": "800", "roundTurnoverQuoteMax": "1200"})
        updated = api.patch(f"/api/v1/strategies/{strategy_id}", json=changed)
        instances = api.get("/api/v1/instances").json()

    assert created_strategy.status_code == 201
    assert strategy_id.startswith("strategy-")
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["strategyId"] == strategy_id
    assert second.json()["strategyId"] == strategy_id
    assert updated.status_code == 200
    assert updated.json()["id"] == strategy_id
    assert updated.json()["version"] == 2
    assert updated.json()["targetVolumeQuote"] == "25000"
    assert updated.json()["roundTurnoverQuoteMin"] == "800"
    assert {instance["strategy"]["name"] for instance in instances} == {"25k shared"}
    assert {instance["strategyId"] for instance in instances} == {strategy_id}

def test_bulk_strategy_assignment_resets_strategy_progress_but_preserves_execution_sequence_and_audit() -> None:
    app = create_app(ControlPlaneSettings(seed_demo_data=False))
    with TestClient(app) as api:
        source = api.post("/api/v1/strategies", json=strategy_payload(name="source")).json()
        target = api.post(
            "/api/v1/strategies",
            json=strategy_payload(name="target", target="10000"),
        ).json()
        payload = create_payload()
        payload["strategyId"] = source["id"]
        created = api.post("/api/v1/instances", json=payload).json()
        instance = app.state.fleet_repository.get(created["id"])
        app.state.fleet_repository.replace(
            instance.model_copy(
                update={
                    "cycle": instance.cycle.model_copy(update={"completed": 7, "target": 20}),
                    "strategy_progress": instance.strategy_progress.model_copy(
                        update={
                            "generated_volume_quote": Decimal("3200"),
                            "started_at_ms": int(time.time() * 1000),
                            "system_pause_reason": "beta:beta_timeout",
                        }
                    ),
                },
                deep=True,
            )
        )
        audit_plan = PairCyclePlan(
            cycle_id="cycle-before-reassignment",
            sequence=7,
            total_quote=Decimal("100"),
            btc_long_quote=Decimal("60"),
            eth_short_quote=Decimal("40"),
            allocation_version="beta-before-reassignment",
        )
        app.state.execution_journal.begin(created["id"], audit_plan)
        app.state.execution_journal.finish(
            audit_plan.cycle_id,
            CycleExecutionStatus.COMPLETED,
            "mock_pair_filled",
        )

        assigned = api.post(
            f"/api/v1/strategies/{target['id']}/assign",
            json={"instanceIds": [created["id"]]},
        )
        protected = api.delete(f"/api/v1/strategies/{target['id']}")
        unused = api.delete(f"/api/v1/strategies/{source['id']}")

    body = assigned.json()
    assert assigned.status_code == 200
    assert body["strategy"]["id"] == target["id"]
    assert body["instances"][0]["strategyProgress"]["generatedVolumeQuote"] == "0"
    assert body["instances"][0]["strategyProgress"]["startedAtMs"] is None
    assert body["instances"][0]["strategyProgress"]["systemPauseReason"] is None
    assert body["instances"][0]["cycle"]["completed"] == 7
    assert body["instances"][0]["cycle"]["target"] > 7
    assert app.state.execution_journal.find(created["id"], 7).plan.cycle_id == "cycle-before-reassignment"
    assert protected.status_code == 409
    assert unused.status_code == 204

def test_strategy_edit_and_assignment_require_stopped_accounts_without_open_pairs() -> None:
    app = create_app(ControlPlaneSettings(seed_demo_data=False, mock_tick_interval_seconds=60))
    with TestClient(app) as api:
        first = api.post("/api/v1/strategies", json=strategy_payload(name="first")).json()
        second = api.post("/api/v1/strategies", json=strategy_payload(name="second")).json()
        payload = create_payload()
        payload["strategyId"] = first["id"]
        instance_id = api.post("/api/v1/instances", json=payload).json()["id"]
        api.post(f"/api/v1/instances/{instance_id}/actions/start")

        edit = api.patch(f"/api/v1/strategies/{first['id']}", json=strategy_payload(name="changed"))
        assign = api.post(
            f"/api/v1/strategies/{second['id']}/assign",
            json={"instanceIds": [instance_id]},
        )

    assert edit.status_code == 409
    assert "stop instance" in edit.json()["detail"]
    assert assign.status_code == 409
    assert "stop instance" in assign.json()["detail"]

def test_stopped_instance_can_update_independent_mock_execution_settings() -> None:
    app = create_app(ControlPlaneSettings(seed_demo_data=False))
    configured = create_payload()
    configured["cycleTarget"] = 125
    configured["mockCycleTotalQuote"] = "12.50"

    with TestClient(app) as api:
        created = api.post("/api/v1/instances", json=configured)
        instance_id = created.json()["id"]
        updated = api.patch(
            f"/api/v1/instances/{instance_id}",
            json={"cycleTarget": 175, "mockCycleTotalQuote": "37.50"},
        )

        persisted = app.state.fleet_repository.get(instance_id)
        app.state.fleet_repository.replace(
            persisted.model_copy(
                update={"cycle": persisted.cycle.model_copy(update={"completed": 176})},
                deep=True,
            )
        )
        below_completed = api.patch(
            f"/api/v1/instances/{instance_id}",
            json={"cycleTarget": 175},
        )

    assert created.status_code == 201
    assert created.json()["cycle"]["target"] == 125
    assert created.json()["mockCycleTotalQuote"] == "12.50"
    assert updated.status_code == 200
    assert updated.json()["cycle"]["target"] == 175
    assert updated.json()["mockCycleTotalQuote"] == "37.50"
    assert below_completed.status_code == 422
    assert below_completed.json()["detail"] == "cycle target cannot be lower than completed cycles"

def test_history_start_is_returned_persisted_and_can_be_cleared() -> None:
    app = create_app(ControlPlaneSettings(seed_demo_data=False))
    history_start = int(time.time() * 1000) - 86_400_000
    payload = create_payload()
    payload["historyStartAtMs"] = history_start

    with TestClient(app) as api:
        created = api.post("/api/v1/instances", json=payload)
        instance_id = created.json()["id"]
        cleared = api.patch(f"/api/v1/instances/{instance_id}", json={"historyStartAtMs": None})

    assert created.status_code == 201
    assert created.json()["historyStartAtMs"] == history_start
    assert cleared.status_code == 200
    assert cleared.json()["historyStartAtMs"] is None
    assert app.state.fleet_repository.get(instance_id).history_start_at_ms is None

def test_future_history_start_is_rejected() -> None:
    payload = create_payload()
    payload["historyStartAtMs"] = int(time.time() * 1000) + 60_000

    response = client().post("/api/v1/instances", json=payload)

    assert response.status_code == 422
    assert "history start cannot be in the future" in response.json()["detail"][0]["msg"]
