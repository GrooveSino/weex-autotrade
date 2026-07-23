from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from fleet_api.config import ControlPlaneSettings
from fleet_api.execution import CycleExecutionStatus, PairCyclePlan
from fleet_api.main import create_app
from fleet_api.models import (
    CreateInstanceRequest,
    InstanceStatus,
    UpdateInstanceRequest,
)
from fleet_api.repository import InMemoryAccountRepository
from fleet_api.service import FleetControlService
from fleet_api.vault import EphemeralCredentialVault

from .test_api_support import (
    ExpectedWriteFailure,
    FailingReplaceRepository,
    client,
    create_payload,
)


def test_execution_history_is_account_scoped_read_only_and_marks_uncertain_cycles() -> None:
    app = create_app(ControlPlaneSettings(seed_demo_data=False))
    with TestClient(app) as api:
        instance_id = api.post("/api/v1/instances", json=create_payload()).json()["id"]
        other_instance_id = api.post("/api/v1/instances", json=create_payload()).json()["id"]
        plan = PairCyclePlan(
            cycle_id="cycle-audit-1",
            sequence=1,
            total_quote=Decimal("20"),
            btc_long_quote=Decimal("10"),
            eth_short_quote=Decimal("10"),
            allocation_version="test-existing-v1",
        )
        app.state.execution_journal.begin(instance_id, plan)
        app.state.execution_journal.finish(
            plan.cycle_id,
            CycleExecutionStatus.UNCERTAIN,
            "adapter_exception:connectionerror",
        )
        other_plan = PairCyclePlan(
            cycle_id="cycle-other-account",
            sequence=1,
            total_quote=Decimal("20"),
            btc_long_quote=Decimal("10"),
            eth_short_quote=Decimal("10"),
            allocation_version="test-existing-v1",
        )
        app.state.execution_journal.begin(other_instance_id, other_plan)
        app.state.execution_journal.finish(
            other_plan.cycle_id,
            CycleExecutionStatus.COMPLETED,
            "mock_pair_filled",
        )

        response = api.get(f"/api/v1/instances/{instance_id}/executions")
        missing = api.get("/api/v1/instances/missing/executions")

    assert response.status_code == 200
    assert response.json() == [
        {
            "cycleId": "cycle-audit-1",
            "sequence": 1,
            "status": "uncertain",
            "reason": "adapter_exception:connectionerror",
            "totalQuote": "20",
            "turnoverQuote": "40",
            "btcLongQuote": "10",
            "ethShortQuote": "10",
            "allocationVersion": "test-existing-v1",
            "positionHoldSeconds": 0,
            "roundIntervalSeconds": 0,
            "sizingMode": "legacy_fixed",
            "strategyId": "legacy",
            "createdAtMs": response.json()[0]["createdAtMs"],
            "updatedAtMs": response.json()[0]["updatedAtMs"],
            "reconciliationRequired": True,
            "retryAllowed": False,
        }
    ]
    assert missing.status_code == 404

def test_strategy_run_history_is_session_scoped_and_hides_cycle_details() -> None:
    app = create_app(ControlPlaneSettings(seed_demo_data=False))
    with TestClient(app) as api:
        created = api.post("/api/v1/instances", json=create_payload()).json()
        ledger = app.state.trade_volume_ledger
        session = app.state.session_volume
        session.start(
            session_id="history-run",
            account_id=created["id"],
            mode="demo",
            started_at_ms=1_000,
            target_quote_volume=Decimal("25"),
            strategy_id=created["strategyId"],
            strategy_name=created["strategy"]["name"],
            strategy_version=created["strategy"]["version"],
            target_mode="incremental",
            strategy_target_quote_volume=Decimal("25"),
            baseline_lifetime_quote_volume=Decimal("100"),
            starting_available_balance_quote=Decimal("42.50"),
        )
        ledger.update_session("history-run", source_complete=True, stale=False, pending_sync=False)
        session.finalize(
            "history-run",
            result="stopped",
            reason="manual_stop",
            finished_at_ms=2_000,
            final_lifetime_quote_volume=Decimal("100"),
            ending_available_balance_quote=Decimal("42.17"),
        )
        response = api.get(f"/api/v1/instances/{created['id']}/strategy-runs?limit=10")

    assert response.status_code == 200
    body = response.json()
    assert body["nextCursor"] is None
    assert body["items"][0]["sessionId"] == "history-run"
    assert body["items"][0]["result"] == "stopped"
    assert body["items"][0]["startingAvailableBalanceQuote"] == "42.50"
    assert body["items"][0]["endingAvailableBalanceQuote"] == "42.17"
    assert body["items"][0]["availableBalanceChangeQuote"] == "-0.33"
    assert "cycleId" not in body["items"][0]
    assert "orderId" not in body["items"][0]

def test_demo_instance_lifecycle_and_exact_global_stop() -> None:
    with client() as api:
        instance_id = api.post("/api/v1/instances", json=create_payload()).json()["id"]
        started = api.post(f"/api/v1/instances/{instance_id}/actions/start")
        assert started.status_code == 200
        assert started.json()["status"] == "running"

        rejected = api.post("/api/v1/actions/stop-all", json={"confirmation": "stop all"})
        assert rejected.status_code == 409

        stopped = api.post("/api/v1/actions/stop-all", json={"confirmation": "STOP ALL"})
        assert stopped.status_code == 200
        assert stopped.json() == {"stopped": 1, "cancelVerified": 1, "cancelFailed": 0}
        assert api.get(f"/api/v1/instances/{instance_id}").json()["status"] == "stopped"

def test_live_instance_cannot_start_with_mock_adapter() -> None:
    with client() as api:
        instance_id = api.post("/api/v1/instances", json=create_payload(mode="live")).json()["id"]
        response = api.post(f"/api/v1/instances/{instance_id}/actions/start")

    assert response.status_code == 409
    assert "cannot start" in response.json()["detail"]

def test_stopped_instance_can_replace_proxy_without_echoing_credentials() -> None:
    app = create_app(ControlPlaneSettings(seed_demo_data=False))
    with TestClient(app) as api:
        instance_id = api.post("/api/v1/instances", json=create_payload()).json()["id"]
        response = api.patch(
            f"/api/v1/instances/{instance_id}",
            json={
                "name": "Updated 01",
                "accountTag": "updated",
                "proxy": {
                    "type": "socks5",
                    "url": "new-user:new-proxy-secret@proxy.example.com:1080",
                },
            },
        )

    assert response.status_code == 200
    assert response.json()["name"] == "Updated 01"
    assert response.json()["proxy"]["host"] == "proxy.example.com:1080"
    assert "new-proxy-secret" not in response.text
    material = app.state.credential_vault.get(instance_id)
    assert material is not None
    assert material.proxy_url.get_secret_value().endswith("proxy.example.com:1080")

def test_stopped_instance_keeps_stored_credentials_when_edit_payload_omits_them() -> None:
    app = create_app(ControlPlaneSettings(seed_demo_data=False))
    with TestClient(app) as api:
        instance_id = api.post("/api/v1/instances", json=create_payload()).json()["id"]
        before = app.state.credential_vault.get(instance_id)
        response = api.patch(
            f"/api/v1/instances/{instance_id}",
            json={"name": "Renamed without credential rotation", "accountTag": "edited"},
        )
        after = app.state.credential_vault.get(instance_id)

    assert response.status_code == 200
    assert response.json()["name"] == "Renamed without credential rotation"
    assert "apiSecret" not in response.text
    assert before is not None and after is not None
    assert after.api_key.get_secret_value() == before.api_key.get_secret_value()
    assert after.api_secret.get_secret_value() == before.api_secret.get_secret_value()
    assert after.passphrase.get_secret_value() == before.passphrase.get_secret_value()

def test_stopped_instance_can_disable_proxy_without_replacing_credentials() -> None:
    app = create_app(ControlPlaneSettings(seed_demo_data=False))
    with TestClient(app) as api:
        instance_id = api.post("/api/v1/instances", json=create_payload()).json()["id"]
        before = app.state.credential_vault.get(instance_id)
        response = api.patch(f"/api/v1/instances/{instance_id}", json={"proxy": {"type": "none"}})
        after = app.state.credential_vault.get(instance_id)

    assert response.status_code == 200
    assert response.json()["proxy"]["type"] == "none"
    assert response.json()["proxy"]["host"] == "不使用代理"
    assert before is not None and after is not None
    assert after.proxy_url is None
    assert after.api_key.get_secret_value() == before.api_key.get_secret_value()
    assert after.api_secret.get_secret_value() == before.api_secret.get_secret_value()
    assert after.passphrase.get_secret_value() == before.passphrase.get_secret_value()

def test_error_instance_can_recover_http_proxy_without_changing_strategy() -> None:
    repository = InMemoryAccountRepository()
    vault = EphemeralCredentialVault()
    service = FleetControlService(repository, vault)
    created = service.create_instance(CreateInstanceRequest.model_validate(create_payload(mode="live")))
    repository.replace(created.model_copy(update={"status": InstanceStatus.ERROR}))

    updated = service.update_instance(
        created.id,
        UpdateInstanceRequest.model_validate(
            {
                "proxy": {
                    "type": "http",
                    "url": "proxy.example.com:8080:user:password",
                }
            }
        ),
    )

    assert updated.status is InstanceStatus.ERROR
    assert updated.proxy.type.value == "http"
    assert updated.strategy_id == created.strategy_id
    material = vault.get(created.id)
    assert material is not None
    assert material.proxy_url.get_secret_value() == "http://user:password@proxy.example.com:8080"

def test_update_restores_credentials_when_public_account_write_fails() -> None:
    repository = FailingReplaceRepository()
    vault = EphemeralCredentialVault()
    service = FleetControlService(repository, vault)
    created = service.create_instance(CreateInstanceRequest.model_validate(create_payload()))
    original = vault.get(created.id)
    repository.fail_next_replace = True

    with pytest.raises(ExpectedWriteFailure, match="repository replace failed"):
        service.update_instance(
            created.id,
            UpdateInstanceRequest.model_validate(
                {
                    "credentials": {
                        "apiKey": "replacement-api-key-WXYZ",
                        "apiSecret": "replacement-secret",
                        "passphrase": "replacement-passphrase",
                    }
                }
            ),
        )

    restored = vault.get(created.id)
    assert restored == original
    assert service.get_instance(created.id).api_key_tail == "ABCD"

def test_running_instance_configuration_is_immutable() -> None:
    with client() as api:
        instance_id = api.post("/api/v1/instances", json=create_payload()).json()["id"]
        api.post(f"/api/v1/instances/{instance_id}/actions/start")
        response = api.patch(f"/api/v1/instances/{instance_id}", json={"name": "Unsafe rename"})

    assert response.status_code == 409
    assert "stop the instance" in response.json()["detail"]

def test_running_instance_must_stop_before_delete() -> None:
    with client() as api:
        instance_id = api.post("/api/v1/instances", json=create_payload()).json()["id"]
        api.post(f"/api/v1/instances/{instance_id}/actions/start")
        assert api.delete(f"/api/v1/instances/{instance_id}").status_code == 409
        api.post(f"/api/v1/instances/{instance_id}/actions/stop")
        assert api.delete(f"/api/v1/instances/{instance_id}").status_code == 204
        assert api.get(f"/api/v1/instances/{instance_id}").status_code == 404
