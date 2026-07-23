from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from fleet_api.api_proxy import create_app as create_proxy_app
from fleet_api.auth import LocalUserRegistry
from fleet_api.config import ControlPlaneSettings
from fleet_api.main import create_app
from fleet_api.models import AccountInstance, InstanceStatus, ProxySnapshot, ProxyType, TradingMode
from fleet_api.ownership import owner_scope
from fleet_api.repository import SQLiteAccountRepository


def _write_users(path: Path) -> LocalUserRegistry:
    path.write_text(
        "[users.gg]\npassword = \"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\"\n"
        "[users.colin]\npassword = \"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\"\n",
        encoding="utf-8",
    )
    path.chmod(0o600)
    return LocalUserRegistry(path)


def _strategy_payload(name: str) -> dict[str, object]:
    return {
        "name": name,
        "targetVolumeQuote": "100",
        "roundTurnoverQuoteMin": "10",
        "roundTurnoverQuoteMax": "10",
        "positionHoldMinSeconds": 1,
        "positionHoldMaxSeconds": 1,
        "roundIntervalMinSeconds": 1,
        "roundIntervalMaxSeconds": 1,
    }


def test_proxy_authenticates_cookie_and_forwards_only_authenticated_owner(tmp_path: Path) -> None:
    registry = _write_users(tmp_path / "users.toml")
    received: list[httpx.Request] = []

    def executor(request: httpx.Request) -> httpx.Response:
        received.append(request)
        return httpx.Response(200, json=[])

    app = create_proxy_app(
        Path("/tmp/fleet-executor.sock"),
        transport=httpx.MockTransport(executor),
        user_registry=registry,
        auth_required=True,
    )
    with TestClient(app) as client:
        assert client.get("/api/v1/instances").status_code == 401
        response = client.post("/api/v1/auth/login", json={"username": "colin", "password": "b" * 32})
        assert response.status_code == 200
        assert response.json() == {"userId": "colin"}
        assert "b" * 32 not in response.text
        assert client.get("/api/v1/instances").status_code == 200
    assert received[-1].headers["x-fleet-user"] == "colin"


def test_executor_repository_scopes_instances_and_strategies_to_authenticated_user() -> None:
    settings = ControlPlaneSettings(seed_demo_data=False, local_user_auth_required=True)
    app = create_app(settings, require_command_id=True)
    with TestClient(app) as client:
        gg_headers = {"X-Fleet-User": "gg", "X-Fleet-Command-Id": "gg-strategy"}
        colin_headers = {"X-Fleet-User": "colin", "X-Fleet-Command-Id": "colin-strategy"}
        gg = client.post("/api/v1/strategies", json=_strategy_payload("GG 策略"), headers=gg_headers)
        colin = client.post("/api/v1/strategies", json=_strategy_payload("Colin 策略"), headers=colin_headers)
        assert gg.status_code == 201
        assert colin.status_code == 201
        assert "GG 策略" in {
            item["name"] for item in client.get("/api/v1/strategies", headers={"X-Fleet-User": "gg"}).json()
        }
        colin_strategies = client.get("/api/v1/strategies", headers={"X-Fleet-User": "colin"}).json()
        assert {item["name"] for item in colin_strategies} == {"Colin 策略"}
        assert gg.json()["id"] not in {
            item["id"] for item in client.get("/api/v1/strategies", headers={"X-Fleet-User": "colin"}).json()
        }


def test_legacy_sqlite_rows_migrate_to_gg_without_rewriting_exchange_material(tmp_path: Path) -> None:
    database = tmp_path / "fleet.db"
    legacy = sqlite3.connect(database)
    legacy.executescript(
        """
        CREATE TABLE instances (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
        CREATE TABLE instance_logs (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            instance_id TEXT NOT NULL,
            payload TEXT NOT NULL
        );
        CREATE TABLE instance_log_reads (instance_id TEXT PRIMARY KEY, reads INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE strategies (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
        """
    )
    instance = AccountInstance(
        id="legacy-account",
        name="Legacy",
        account_tag="legacy",
        api_key_tail="ABCD",
        mode=TradingMode.DEMO,
        status=InstanceStatus.STOPPED,
        phase="legacy",
        proxy=ProxySnapshot(type=ProxyType.NONE, host="不使用代理"),
    )
    payload = instance.model_dump(mode="json", by_alias=True)
    payload.pop("ownerUserId")
    strategy = payload["strategy"]
    assert isinstance(strategy, dict)
    strategy.pop("ownerUserId")
    legacy.execute("INSERT INTO instances(id, payload) VALUES (?, ?)", (instance.id, json.dumps(payload)))
    legacy.commit()
    legacy.close()

    repository = SQLiteAccountRepository(database)
    try:
        with owner_scope("gg"):
            restored = repository.get(instance.id)
            assert restored is not None
            assert restored.owner_user_id == "gg"
            assert restored.strategy.owner_user_id == "gg"
        with owner_scope("colin"):
            assert repository.get(instance.id) is None
    finally:
        repository.close()
