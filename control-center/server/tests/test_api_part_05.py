import time
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

import fleet_api.campaigns as campaigns_module
from fleet_api.config import ControlPlaneSettings
from fleet_api.main import create_app
from fleet_api.models import (
    CreateInstanceRequest,
)
from fleet_api.repository import InMemoryAccountRepository
from fleet_api.service import FleetControlService
from fleet_api.volume_history import NormalizedTradeFill, utc_day_start_ms

from .test_api_support import (
    ExpectedWriteFailure,
    FailingCredentialVault,
    client,
    create_payload,
    monitor_campaign,
    strategy_payload,
)


def test_readonly_metadata_update_keeps_existing_trade_history() -> None:
    now_ms = int(time.time() * 1000)
    app = create_app(ControlPlaneSettings(adapter="weex-readonly", seed_demo_data=False))
    payload = create_payload(mode="live")
    payload["historyStartAtMs"] = now_ms - 86_400_000

    with TestClient(app) as api:
        created = api.post("/api/v1/instances", json=payload).json()
        ledger = app.state.trade_volume_ledger
        ledger.record(
            created["id"],
            (NormalizedTradeFill("existing-fill", now_ms, Decimal("25"), "BTCUSDT"),),
        )
        updated = api.patch(
            f"/api/v1/instances/{created['id']}",
            json={"name": "Renamed", "historyStartAtMs": payload["historyStartAtMs"]},
        )
        aggregate = ledger.aggregate(created["id"], utc_day_start_ms(now_ms))

    assert updated.status_code == 200
    assert aggregate.lifetime == Decimal("25")
    assert aggregate.fill_count == 1

@pytest.mark.parametrize("change", ["credentials", "history-start"])
def test_readonly_identity_or_history_change_resets_derived_trade_history(change: str) -> None:
    now_ms = int(time.time() * 1000)
    app = create_app(ControlPlaneSettings(adapter="weex-readonly", seed_demo_data=False))
    payload = create_payload(mode="live")
    payload["historyStartAtMs"] = now_ms - 2 * 86_400_000

    with TestClient(app) as api:
        created = api.post("/api/v1/instances", json=payload).json()
        instance_id = created["id"]
        ledger = app.state.trade_volume_ledger
        ledger.record(
            instance_id,
            (NormalizedTradeFill("old-account-fill", now_ms, Decimal("30"), "ETHUSDT"),),
        )
        ledger.set_complete(instance_id, True)
        patch = (
            {
                "credentials": {
                    "apiKey": "replacement-key-EFGH",
                    "apiSecret": "replacement-secret",
                    "passphrase": "replacement-passphrase",
                }
            }
            if change == "credentials"
            else {"historyStartAtMs": now_ms - 86_400_000}
        )
        updated = api.patch(f"/api/v1/instances/{instance_id}", json=patch)
        aggregate = ledger.aggregate(instance_id, utc_day_start_ms(now_ms))

    assert updated.status_code == 200
    assert updated.json()["phase"] == "历史口径已变更，等待重新扫描"
    assert updated.json()["wallet"] == {"equity": 0.0, "available": 0.0, "unrealizedPnl": 0.0}
    assert updated.json()["volume"] == {"lifetime": 0.0, "today": 0.0, "complete": False}
    assert aggregate.lifetime == 0
    assert aggregate.fill_count == 0
    assert aggregate.complete is False

def test_create_removes_public_account_when_credential_write_fails() -> None:
    repository = InMemoryAccountRepository()
    service = FleetControlService(repository, FailingCredentialVault())

    with pytest.raises(ExpectedWriteFailure, match="vault write failed"):
        service.create_instance(CreateInstanceRequest.model_validate(create_payload()))

    assert repository.list() == []

def test_validation_errors_do_not_echo_invalid_credential_input() -> None:
    payload = create_payload()
    payload["credentials"] = "malformed-secret-payload"
    response = client().post("/api/v1/instances", json=payload)

    assert response.status_code == 422
    assert "malformed-secret-payload" not in response.text

def test_listing_instances_does_not_load_logs() -> None:
    app = create_app(ControlPlaneSettings(seed_demo_data=False))
    with TestClient(app) as api:
        created = api.post("/api/v1/instances", json=create_payload()).json()
        instance_id = created["id"]
        assert app.state.fleet_repository.log_read_count(instance_id) == 0
        assert api.get("/api/v1/instances").status_code == 200
        assert app.state.fleet_repository.log_read_count(instance_id) == 0
        logs = api.get(f"/api/v1/instances/{instance_id}/logs")
        assert logs.status_code == 200
        assert app.state.fleet_repository.log_read_count(instance_id) == 1

def test_incremental_log_updates_are_account_scoped_and_cursor_safe() -> None:
    app = create_app(ControlPlaneSettings(seed_demo_data=False, mock_tick_interval_seconds=60))
    with TestClient(app) as api:
        first = api.post("/api/v1/instances", json=create_payload()).json()
        second_payload = create_payload()
        second_payload["name"] = "Other account"
        second_payload["credentials"] = {
            "apiKey": "other-key-EFGH",
            "apiSecret": "other-secret",
            "passphrase": "other-passphrase",
        }
        second = api.post("/api/v1/instances", json=second_payload).json()

        initial = api.get(f"/api/v1/instances/{first['id']}/log-updates?limit=50")
        initial_body = initial.json()
        cursor = initial_body["cursor"]
        api.post(f"/api/v1/instances/{first['id']}/actions/start")
        api.post(f"/api/v1/instances/{second['id']}/actions/start")
        incremental = api.get(
            f"/api/v1/instances/{first['id']}/log-updates",
            params={"limit": 50, "after": cursor},
        )
        incremental_body = incremental.json()
        no_changes = api.get(
            f"/api/v1/instances/{first['id']}/log-updates",
            params={"limit": 50, "after": incremental_body["cursor"]},
        )
        reset = api.get(
            f"/api/v1/instances/{first['id']}/log-updates",
            params={"limit": 50, "after": "expired-cursor"},
        )

    assert initial.status_code == 200
    assert initial_body["reset"] is False
    assert len(initial_body["lines"]) == 1
    assert incremental.status_code == 200
    assert incremental_body["reset"] is False
    assert [line["message"] for line in incremental_body["lines"]] == ["实例操作已接受：启动"]
    assert all("Other account" not in line["message"] for line in incremental_body["lines"])
    assert no_changes.json() == {"lines": [], "cursor": incremental_body["cursor"], "reset": False}
    assert reset.json()["reset"] is True
    assert reset.json()["cursor"] == incremental_body["cursor"]

def test_clearing_logs_is_account_scoped_and_new_events_continue_from_an_empty_cursor() -> None:
    app = create_app(ControlPlaneSettings(seed_demo_data=False, mock_tick_interval_seconds=60))
    with TestClient(app) as api:
        first = api.post("/api/v1/instances", json=create_payload()).json()
        second_payload = create_payload()
        second_payload["name"] = "Other account"
        second_payload["credentials"] = {
            "apiKey": "other-key-EFGH",
            "apiSecret": "other-secret",
            "passphrase": "other-passphrase",
        }
        second = api.post("/api/v1/instances", json=second_payload).json()
        old_cursor = api.get(f"/api/v1/instances/{first['id']}/log-updates?limit=50").json()["cursor"]

        cleared = api.delete(f"/api/v1/instances/{first['id']}/log-updates")
        first_after_clear = api.get(
            f"/api/v1/instances/{first['id']}/log-updates",
            params={"limit": 50, "after": old_cursor},
        ).json()
        second_logs = api.get(f"/api/v1/instances/{second['id']}/log-updates?limit=50").json()

        app.state.fleet_service.record_campaign_progress(
            first["id"],
            {"name": "campaign_run_started", "run": 1, "fields": {"remaining_quote": "500"}},
        )
        new_logs = api.get(f"/api/v1/instances/{first['id']}/log-updates?limit=50").json()

    assert cleared.status_code == 204
    assert first_after_clear == {"lines": [], "cursor": None, "reset": True}
    assert len(second_logs["lines"]) == 1
    assert [line["message"] for line in new_logs["lines"]] == ["实盘执行：运行 1 开始；剩余目标 500 USDT"]

def test_explicit_refresh_is_visible_in_realtime_logs() -> None:
    app = create_app(ControlPlaneSettings(seed_demo_data=False, mock_tick_interval_seconds=60))
    with TestClient(app) as api:
        created = api.post("/api/v1/instances", json=create_payload()).json()
        initial = api.get(f"/api/v1/instances/{created['id']}/log-updates?limit=50").json()
        refreshed = api.post(f"/api/v1/instances/{created['id']}/refresh")
        updates = api.get(
            f"/api/v1/instances/{created['id']}/log-updates",
            params={"limit": 50, "after": initial["cursor"]},
        ).json()

    assert refreshed.status_code == 200
    assert [line["message"] for line in updates["lines"]] == ["刷新成功：价格、钱包与仓位已同步"]

def test_campaign_progress_is_projected_to_account_log_updates() -> None:
    app = create_app(ControlPlaneSettings(seed_demo_data=False, mock_tick_interval_seconds=60))
    with TestClient(app) as api:
        created = api.post("/api/v1/instances", json=create_payload()).json()
        initial = api.get(f"/api/v1/instances/{created['id']}/log-updates?limit=50").json()
        app.state.fleet_service.record_campaign_progress(
            created["id"],
            {
                "name": "leg_completed",
                "fields": {
                    "symbol": "BTCUSDT",
                    "action": "open",
                    "quote_volume": "250",
                    "fill_count": 2,
                    "api_secret": "must-not-appear",
                },
            },
        )
        updates = api.get(
            f"/api/v1/instances/{created['id']}/log-updates",
            params={"limit": 50, "after": initial["cursor"]},
        ).json()

    assert updates["reset"] is False
    assert len(updates["lines"]) == 1
    assert updates["lines"][0]["level"] == "success"
    assert updates["lines"][0]["message"] == "实盘执行：BTCUSDT open 成交已核验；250 USDT / 2 笔"
    assert "must-not-appear" not in str(updates["lines"])

def test_instance_projection_uses_live_fill_reconciliation_before_ledger_catches_up() -> None:
    app = create_app(ControlPlaneSettings(seed_demo_data=False, mock_tick_interval_seconds=60))
    started_at_ms = int(time.time() * 1000)
    with TestClient(app) as api:
        strategy = api.post("/api/v1/strategies", json=strategy_payload(name="实时策略", target="500"))
        assert strategy.status_code == 201
        payload = create_payload(mode="live")
        payload["strategyId"] = strategy.json()["id"]
        created = api.post("/api/v1/instances", json=payload)
        assert created.status_code == 201
        instance_id = created.json()["id"]
        session_id = "session-main-projection"
        app.state.trade_volume_ledger.create_session(
            session_id,
            instance_id,
            "live",
            started_at_ms,
            Decimal("500"),
        )
        campaign = monitor_campaign(campaign_id="wc-main-projection", created_at_ms=started_at_ms)
        app.state.campaign_journal.create(
            instance_id,
            campaign,
            {"session_id": session_id, "execution_kind": "bound_strategy", "owner_user_id": "gg"},
        )
        for event in (
            {
                "event": "leg_completed",
                "timestamp_ms": started_at_ms + 1_000,
                "round": 1,
                "sequence": 1,
                "symbol": "BTCUSDT",
                "action": "open",
                "quote_volume": "30.25",
                "fill_count": 1,
            },
            {
                "event": "leg_completed",
                "timestamp_ms": started_at_ms + 2_000,
                "round": 1,
                "sequence": 2,
                "symbol": "ETHUSDT",
                "action": "open",
                "quote_volume": "10.75",
                "fill_count": 1,
            },
            {
                "event": "hold_started",
                "timestamp_ms": started_at_ms + 3_000,
                "round": 1,
                "seconds": "30",
            },
        ):
            app.state.campaign_journal.append_and_project(
                campaign.campaign_id,
                campaigns_module._sanitize_event(event),
                owner_user_id="gg",
                account_id=instance_id,
                session_id=session_id,
                executor_generation=app.state.executor_generation,
                projection_version=3,
            )

        response = api.get(f"/api/v1/instances/{instance_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["volume"]["strategyProgressSource"] == "execution_journal"
    assert body["volume"]["strategyVerifiedQuoteVolume"] == "41.00"
    assert body["volume"]["strategyRemainingQuoteVolume"] == "459.00"
    assert body["strategyProgress"]["stage"] == "holding"
    assert body["strategyProgress"]["nextActionAtMs"] == started_at_ms + 33_000
