import time
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

import fleet_api.main as main_module
from fleet_api.config import ControlPlaneSettings
from fleet_api.execution import CycleExecutionStatus, PairCyclePlan
from fleet_api.main import create_app
from fleet_api.models import (
    CreateInstanceRequest,
    ExposureSnapshot,
    InstanceStatus,
    StrategyTargetMode,
    UpdateInstanceRequest,
)
from fleet_api.repository import InMemoryAccountRepository
from fleet_api.service import FleetControlService
from fleet_api.vault import CredentialMaterial, EphemeralCredentialVault
from fleet_api.volume_history import NormalizedTradeFill, utc_day_start_ms


class ExpectedWriteFailure(RuntimeError):
    pass


class FailingCredentialVault(EphemeralCredentialVault):
    def put(self, instance_id: str, material: CredentialMaterial) -> None:
        raise ExpectedWriteFailure("vault write failed")


class FailingReplaceRepository(InMemoryAccountRepository):
    fail_next_replace = False

    def replace(self, instance):
        if self.fail_next_replace:
            self.fail_next_replace = False
            raise ExpectedWriteFailure("repository replace failed")
        return super().replace(instance)


class StaticBetaMarketProvider:
    async def market_snapshot(self) -> dict[str, object]:
        return {
            "schemaVersion": "1.0",
            "strategy": "btc_long_eth_short",
            "status": "low_confidence",
            "upstreamUsable": False,
            "reasonCodes": ["confidence_below_threshold"],
            "finalBeta": "0.44260456370165036",
            "btcLongRatio": "1.0",
            "ethShortRatio": "0.44260456370165036",
            "btcLongWeight": "0.6931906533236318",
            "ethShortWeight": "0.30680934667636806",
            "confidence": "0.60",
            "confidenceThreshold": "0.65",
            "source": "beta_v2",
            "asOfMs": 1784377856564,
            "generatedAtMs": 1784377856889,
            "ageMs": "324",
            "maxAgeMs": "10000",
        }


class RefreshTrackingBetaProvider:
    def __init__(self) -> None:
        self.refresh_calls = 0
        self.closed = False

    async def refresh(self) -> bool:
        self.refresh_calls += 1
        return True

    @staticmethod
    def seconds_until_refresh(maximum_seconds: float) -> float:
        return maximum_seconds

    async def aclose(self) -> None:
        self.closed = True


def client() -> TestClient:
    return TestClient(create_app(ControlPlaneSettings(seed_demo_data=False)))


def create_payload(*, mode: str = "demo") -> dict[str, object]:
    return {
        "name": "Test 01",
        "accountTag": "pytest",
        "mode": mode,
        "credentials": {
            "apiKey": "key-super-secret-ABCD",
            "apiSecret": "secret-never-return",
            "passphrase": "pass-never-return",
        },
        "proxy": {
            "type": "https",
            "url": "proxy-user:proxy-password@proxy.example.com:9341",
        },
    }


def strategy_payload(*, name: str = "20k shared", target: str = "20000") -> dict[str, object]:
    return {
        "name": name,
        "targetVolumeQuote": target,
        "roundTurnoverQuoteMin": "500",
        "roundTurnoverQuoteMax": "750",
        "positionHoldMinSeconds": 300,
        "positionHoldMaxSeconds": 900,
        "roundIntervalMinSeconds": 600,
        "roundIntervalMaxSeconds": 1800,
    }


def test_strategy_target_mode_and_funding_preflight_are_exposed_and_enforced() -> None:
    with client() as api:
        payload = strategy_payload(name="Impossible round", target="200000")
        payload.update(
            {
                "targetMode": "lifetime",
                "roundTurnoverQuoteMin": "200000",
                "roundTurnoverQuoteMax": "200000",
            }
        )
        strategy_response = api.post("/api/v1/strategies", json=payload)
        assert strategy_response.status_code == 201
        strategy = strategy_response.json()
        account_payload = create_payload()
        account_payload["strategyId"] = strategy["id"]
        created = api.post("/api/v1/instances", json=account_payload)
        assert created.status_code == 201
        snapshot = created.json()

        assert strategy["targetMode"] == "lifetime"
        assert snapshot["fundingPreflight"]["status"] == "insufficient"
        assert snapshot["fundingPreflight"]["requiredLeverage"] > 99
        rejected = api.post(f"/api/v1/instances/{snapshot['id']}/actions/start")

    assert rejected.status_code == 409
    assert "funding preflight failed" in rejected.json()["detail"]


def test_lifetime_target_requires_complete_trade_history_before_start() -> None:
    app = create_app(ControlPlaneSettings(seed_demo_data=False))
    with TestClient(app) as api:
        created = api.post("/api/v1/instances", json=create_payload()).json()
        instance = app.state.fleet_repository.get(created["id"])
        app.state.fleet_repository.replace(
            instance.model_copy(
                update={
                    "strategy": instance.strategy.model_copy(update={"target_mode": StrategyTargetMode.LIFETIME}),
                    "volume": instance.volume.model_copy(update={"complete": False}),
                },
                deep=True,
            )
        )
        rejected = api.post(f"/api/v1/instances/{created['id']}/actions/start")

    assert rejected.status_code == 409
    assert "complete lifetime trade history" in rejected.json()["detail"]


def test_health_proves_live_trading_is_disabled() -> None:
    response = client().get("/api/v1/health")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "adapter": "mock",
        "storage": "memory",
        "liveTradingEnabled": False,
        "executionEnabled": True,
        "liveCampaignsEnabled": False,
        "liveCampaignWorkerCount": 0,
    }


def test_lifespan_runs_one_central_beta_refresher_at_the_configured_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = RefreshTrackingBetaProvider()

    def provider_factory(*_args, **kwargs):
        assert kwargs["cache_seconds"] == 0.01
        assert kwargs["network_on_demand"] is False
        return provider

    monkeypatch.setattr(main_module, "HttpBetaAllocationProvider", provider_factory)
    app = main_module.create_app(
        ControlPlaneSettings(
            seed_demo_data=False,
            mock_tick_interval_seconds=60,
            beta_refresh_interval_seconds=0.01,
            beta_background_refresh_enabled=True,
        )
    )

    with TestClient(app):
        time.sleep(0.035)

    assert provider.refresh_calls >= 3
    assert provider.closed is True


def test_readonly_adapter_exposes_health_but_rejects_execution_actions() -> None:
    app = create_app(ControlPlaneSettings(adapter="weex-readonly", seed_demo_data=False))
    with TestClient(app) as api:
        assert api.get("/api/v1/health").json() == {
            "status": "ok",
            "adapter": "weex-readonly",
            "storage": "memory",
            "liveTradingEnabled": False,
            "executionEnabled": False,
            "liveCampaignsEnabled": False,
            "liveCampaignWorkerCount": 0,
        }
        created = api.post("/api/v1/instances", json=create_payload(mode="live"))
        assert created.status_code == 201
        assert created.json()["mockCycleTotalQuote"] is None
        instance_id = created.json()["id"]
        start = api.post(f"/api/v1/instances/{instance_id}/actions/start")
        stop = api.post(f"/api/v1/instances/{instance_id}/actions/stop")

    assert start.status_code == 409
    assert "read-only" in start.json()["detail"]
    assert stop.status_code == 200
    assert stop.json()["status"] == "stopped"


def test_mock_close_positions_endpoint_flattens_only_a_non_running_exposed_instance() -> None:
    app = create_app(ControlPlaneSettings(seed_demo_data=True))
    with TestClient(app) as api:
        before = api.get("/api/v1/instances/ins-api-02").json()
        response = api.post("/api/v1/instances/ins-api-02/positions/close")

    assert response.status_code == 200
    closed = response.json()
    assert closed["status"] == "warning"
    assert closed["exposure"] == {"btcLong": 0.0, "ethShort": 0.0}
    assert closed["volume"]["lifetime"] == pytest.approx(before["volume"]["lifetime"] + 938.1)
    assert closed["volume"]["today"] == pytest.approx(before["volume"]["today"] + 938.1)
    assert closed["strategyProgress"]["generatedVolumeQuote"] == "3738.1"
    assert "一键平仓完成" in closed["phase"]


def test_close_positions_endpoint_rejects_running_and_flat_instances() -> None:
    app = create_app(ControlPlaneSettings(seed_demo_data=True))
    with TestClient(app) as api:
        running = api.post("/api/v1/instances/ins-api-01/positions/close")
        created = api.post("/api/v1/instances", json=create_payload()).json()
        flat = api.post(f"/api/v1/instances/{created['id']}/positions/close")

    assert running.status_code == 409
    assert "stop or pause" in running.json()["detail"]
    assert flat.status_code == 409
    assert "no open positions" in flat.json()["detail"]


def test_readonly_adapter_rejects_position_close_even_when_exposure_exists() -> None:
    app = create_app(ControlPlaneSettings(adapter="weex-readonly", seed_demo_data=False))
    with TestClient(app) as api:
        created = api.post("/api/v1/instances", json=create_payload(mode="live")).json()
        instance = app.state.fleet_repository.get(created["id"])
        assert instance is not None
        app.state.fleet_repository.replace(
            instance.model_copy(
                update={
                    "status": InstanceStatus.STOPPED,
                    "exposure": ExposureSnapshot(btc_long=100, eth_short=44),
                },
                deep=True,
            )
        )
        response = api.post(f"/api/v1/instances/{created['id']}/positions/close")

    assert response.status_code == 409
    assert "read-only" in response.json()["detail"]
    unchanged = app.state.fleet_repository.get(created["id"])
    assert unchanged is not None
    assert unchanged.exposure == ExposureSnapshot(btc_long=100, eth_short=44)


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
