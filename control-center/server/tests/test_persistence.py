import json
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import SecretStr

from fleet_api.config import ControlPlaneSettings
from fleet_api.execution import CycleExecutionStatus, PairCyclePlan
from fleet_api.main import create_app
from fleet_api.vault import CredentialVaultError, EncryptedSQLiteCredentialVault


def settings(path: Path, key: str) -> ControlPlaneSettings:
    return ControlPlaneSettings(
        storage="sqlite",
        sqlite_path=path,
        master_key=SecretStr(key),
        seed_demo_data=False,
        mock_tick_interval_seconds=60,
    )


def create_payload() -> dict[str, object]:
    return {
        "name": "Persistent 01",
        "accountTag": "sqlite",
        "mode": "demo",
        "credentials": {
            "apiKey": "persistent-api-key-ABCD",
            "apiSecret": "persistent-secret-never-plaintext",
            "passphrase": "persistent-passphrase-never-plaintext",
        },
        "proxy": {
            "type": "socks5",
            "url": "proxy-user:proxy-password-never-plaintext@proxy.example.com:1080",
        },
    }


def test_sqlite_mode_requires_explicit_master_key(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="FLEET_MASTER_KEY"):
        ControlPlaneSettings(storage="sqlite", sqlite_path=tmp_path / "fleet.db")


def test_account_and_encrypted_credentials_survive_restart_without_plaintext_on_disk(tmp_path: Path) -> None:
    path = tmp_path / "fleet.db"
    key = Fernet.generate_key().decode()
    first = create_app(settings(path, key))
    configured = create_payload()
    configured["cycleTarget"] = 125
    configured["mockCycleTotalQuote"] = "12.50"
    with TestClient(first) as api:
        created = api.post("/api/v1/instances", json=configured)
        assert created.status_code == 201
        instance_id = created.json()["id"]
        logs = api.get(f"/api/v1/instances/{instance_id}/logs")
        assert logs.status_code == 200

    for artifact in tmp_path.glob("fleet.db*"):
        raw_database = artifact.read_bytes()
        for secret in (
            b"persistent-api-key-ABCD",
            b"persistent-secret-never-plaintext",
            b"persistent-passphrase-never-plaintext",
            b"proxy-password-never-plaintext",
        ):
            assert secret not in raw_database

    second = create_app(settings(path, key))
    with TestClient(second) as api:
        restored = api.get(f"/api/v1/instances/{instance_id}")
        assert restored.status_code == 200
        assert restored.json()["name"] == "Persistent 01"
        assert restored.json()["proxy"]["host"] == "proxy.example.com:1080"
        assert restored.json()["cycle"]["target"] == 125
        assert restored.json()["mockCycleTotalQuote"] == "12.50"
        assert second.state.fleet_repository.log_read_count(instance_id) == 1
        material = second.state.credential_vault.get(instance_id)
        assert material is not None
        assert material.api_secret.get_secret_value() == "persistent-secret-never-plaintext"


def test_legacy_embedded_session_target_is_upgraded_before_repository_validation(tmp_path: Path) -> None:
    path = tmp_path / "fleet.db"
    key = Fernet.generate_key().decode()
    first = create_app(settings(path, key))
    with TestClient(first) as api:
        instance_id = api.post("/api/v1/instances", json=create_payload()).json()["id"]

    with sqlite3.connect(path) as connection:
        row = connection.execute("SELECT payload FROM instances WHERE id = ?", (instance_id,)).fetchone()
        assert row is not None
        payload = json.loads(str(row[0]))
        payload["volume"]["session"] = {
            "sessionId": "legacy-session",
            "accountId": instance_id,
            "mode": "demo",
            "startedAtMs": 1_000,
            "targetQuoteVolume": "500",
            "verifiedQuoteVolume": "500",
            "remainingQuoteVolume": "0",
            "status": "completed",
            "fillCount": 2,
            "openingQuoteVolume": "250",
            "closingQuoteVolume": "250",
            "makerQuoteVolume": "500",
            "takerQuoteVolume": "0",
            "unknownLiquidityQuoteVolume": "0",
            "lastSyncAtMs": 2_000,
            "lastReconciliationAtMs": 2_000,
            "sourceComplete": True,
            "stale": False,
            "reconciliationRequired": False,
            "discrepancyQuoteVolume": "0",
            "retryAllowed": False,
        }
        connection.execute(
            "UPDATE instances SET payload = ? WHERE id = ?",
            (json.dumps(payload), instance_id),
        )

    second = create_app(settings(path, key))
    with TestClient(second):
        restored = second.state.fleet_repository.get(instance_id)
        assert restored is not None
        assert restored.volume.session is not None
        assert restored.volume.session.strategy_target_quote_volume == Decimal("500")


def test_null_legacy_embedded_session_target_is_upgraded_before_repository_validation(tmp_path: Path) -> None:
    path = tmp_path / "fleet.db"
    key = Fernet.generate_key().decode()
    first = create_app(settings(path, key))
    with TestClient(first) as api:
        instance_id = api.post("/api/v1/instances", json=create_payload()).json()["id"]

    with sqlite3.connect(path) as connection:
        row = connection.execute("SELECT payload FROM instances WHERE id = ?", (instance_id,)).fetchone()
        assert row is not None
        payload = json.loads(str(row[0]))
        payload["volume"]["session"] = {
            "sessionId": "legacy-null-session",
            "accountId": instance_id,
            "mode": "demo",
            "startedAtMs": 1_000,
            "strategyTargetQuoteVolume": None,
            "targetQuoteVolume": "125",
            "verifiedQuoteVolume": "0",
            "remainingQuoteVolume": "125",
            "status": "active",
            "fillCount": 0,
            "openingQuoteVolume": "0",
            "closingQuoteVolume": "0",
            "makerQuoteVolume": "0",
            "takerQuoteVolume": "0",
            "unknownLiquidityQuoteVolume": "0",
            "lastSyncAtMs": None,
            "lastReconciliationAtMs": None,
            "sourceComplete": False,
            "stale": True,
            "reconciliationRequired": False,
            "discrepancyQuoteVolume": "0",
            "retryAllowed": False,
        }
        connection.execute(
            "UPDATE instances SET payload = ? WHERE id = ?",
            (json.dumps(payload), instance_id),
        )

    second = create_app(settings(path, key))
    with TestClient(second):
        restored = second.state.fleet_repository.get(instance_id)
        assert restored is not None
        assert restored.volume.session is not None
        assert restored.volume.session.strategy_target_quote_volume == Decimal("125")


def test_wrong_master_key_cannot_decrypt_stored_credentials(tmp_path: Path) -> None:
    path = tmp_path / "fleet.db"
    first_key = Fernet.generate_key().decode()
    first = create_app(settings(path, first_key))
    with TestClient(first) as api:
        instance_id = api.post("/api/v1/instances", json=create_payload()).json()["id"]

    wrong_vault = EncryptedSQLiteCredentialVault(path, SecretStr(Fernet.generate_key().decode()))
    try:
        with pytest.raises(CredentialVaultError, match="cannot be decrypted"):
            wrong_vault.get(instance_id)
    finally:
        wrong_vault.close()

    with pytest.raises(CredentialVaultError, match="cannot be decrypted"):
        create_app(settings(path, Fernet.generate_key().decode()))


def test_cleared_logs_remain_empty_after_sqlite_restart(tmp_path: Path) -> None:
    path = tmp_path / "fleet.db"
    key = Fernet.generate_key().decode()
    first = create_app(settings(path, key))
    with TestClient(first) as api:
        instance_id = api.post("/api/v1/instances", json=create_payload()).json()["id"]
        assert api.get(f"/api/v1/instances/{instance_id}/logs").json()
        assert api.delete(f"/api/v1/instances/{instance_id}/log-updates").status_code == 204
        assert api.get(f"/api/v1/instances/{instance_id}/logs").json() == []

    second = create_app(settings(path, key))
    with TestClient(second) as api:
        assert api.get(f"/api/v1/instances/{instance_id}/logs").json() == []


def test_persisted_running_instance_requires_manual_restart_after_process_restart(tmp_path: Path) -> None:
    path = tmp_path / "fleet.db"
    key = Fernet.generate_key().decode()
    first = create_app(settings(path, key))
    with TestClient(first) as api:
        instance_id = api.post("/api/v1/instances", json=create_payload()).json()["id"]
        started = api.post(f"/api/v1/instances/{instance_id}/actions/start")
        assert started.json()["status"] == "running"

    second = create_app(settings(path, key))
    with TestClient(second) as api:
        restored = api.get(f"/api/v1/instances/{instance_id}")
        logs = api.get(f"/api/v1/instances/{instance_id}/logs").json()

    assert restored.json()["status"] == "stopped"
    assert restored.json()["phase"] == "服务已重启，等待人工启动"
    assert any("需要人工启动" in line["message"] for line in logs)


def test_process_restart_marks_prepared_execution_uncertain_without_resubmission(tmp_path: Path) -> None:
    path = tmp_path / "fleet.db"
    key = Fernet.generate_key().decode()
    first = create_app(settings(path, key))
    with TestClient(first) as api:
        instance_id = api.post("/api/v1/instances", json=create_payload()).json()["id"]
        api.post(f"/api/v1/instances/{instance_id}/actions/start")
        first.state.execution_journal.begin(
            instance_id,
            PairCyclePlan(
                cycle_id="cycle-crash-before-result",
                sequence=1,
                total_quote=Decimal("20"),
                btc_long_quote=Decimal("10"),
                eth_short_quote=Decimal("10"),
                allocation_version="test-existing-v1",
            ),
        )

    second = create_app(settings(path, key))
    record = second.state.execution_journal.find(instance_id, 1)
    with TestClient(second) as api:
        restored = api.get(f"/api/v1/instances/{instance_id}").json()

    assert record is not None
    assert record.status is CycleExecutionStatus.UNCERTAIN
    assert record.reason == "process_restarted_before_terminal_result"
    assert restored["status"] == "stopped"
    assert restored["cycle"]["completed"] == 0
