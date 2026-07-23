import time
from concurrent.futures import Future
from decimal import Decimal

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import SecretStr
from weex_cli.beta_allocation import BetaAllocation
from weex_cli.beta_campaign import BetaVolumeCampaign

import fleet_api.campaigns as campaigns_module
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


class LivePreviewGateway:
    def order_book(self, symbol: str, _limit: int = 5) -> dict[str, object]:
        return {"bids": [["100", "10"]], "asks": [["101", "10"]]}

    def amount_step(self, _symbol: str) -> Decimal:
        return Decimal("0.001")

    def amount_to_precision(self, _symbol: str, amount: Decimal) -> Decimal:
        return amount.quantize(Decimal("0.001"))

    def account_balance_rows(self, _mode: str) -> list[dict[str, str]]:
        return [{"asset": "USDT", "availableBalance": "1000"}]

    def positions(self, _mode: str, _symbol: str) -> list[dict[str, str]]:
        return []

    def open_orders(self, _symbol: str, *, mode: str = "live") -> list[dict[str, str]]:
        return []

    def algo_orders(self, _symbol: str) -> list[dict[str, str]]:
        return []

    def fork(self):
        return self

    def close(self) -> None:
        return None


class LivePreviewProvider:
    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def get(self):
        from weex_cli.beta_allocation import BetaAllocation

        return BetaAllocation(
            beta=Decimal("0.4"),
            btc_long_weight=Decimal("0.7142857142857142857142857143"),
            eth_short_weight=Decimal("0.2857142857142857142857142857"),
            version="fake-beta:1",
            as_of_ms=int(time.time() * 1000),
            confidence=Decimal("1"),
            confidence_threshold=Decimal("0"),
            source="fake",
        )


class UnavailableLivePreviewProvider:
    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def get(self):
        from weex_cli.beta_allocation import BetaUnavailable

        raise BetaUnavailable("beta_request_failed:httperror")


class HeldWorkerExecutor:
    """Accepts work without running it, so API lifecycle tests cannot place orders."""

    def __init__(self) -> None:
        self.submissions = 0

    def submit(self, *_args, **_kwargs) -> Future[None]:
        self.submissions += 1
        return Future()

    def shutdown(self, **_kwargs) -> None:
        return None


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


def monitor_campaign(*, campaign_id: str, created_at_ms: int) -> BetaVolumeCampaign:
    return BetaVolumeCampaign(
        schema_version=3,
        campaign_id=campaign_id,
        created_at_ms=created_at_ms,
        expires_at_ms=created_at_ms + 60_000,
        profile_fingerprint="f" * 64,
        target_turnover_quote=Decimal("500"),
        round_turnover_quote_min=Decimal("40"),
        round_turnover_quote=Decimal("80"),
        max_position_quote=Decimal("120"),
        timeout_seconds=60,
        recovery_attempts=3,
        max_empty_rounds=3,
        cooldown_seconds=0,
        hold_min_seconds=5,
        hold_max_seconds=5,
        round_gap_min_seconds=10,
        round_gap_max_seconds=10,
        max_runs=1,
        leverage=2,
        max_auto_leverage=10,
        margin_buffer=Decimal("1.2"),
        margin_mode="cross",
        allocation=BetaAllocation(
            beta=Decimal("0.4"),
            btc_long_weight=Decimal("0.7"),
            eth_short_weight=Decimal("0.3"),
            version="test-beta:1",
            as_of_ms=created_at_ms,
            confidence=Decimal("1"),
            confidence_threshold=Decimal("0"),
            source="fake",
        ),
    )


def test_strategy_monitor_is_idle_without_a_run_and_never_exposes_credentials() -> None:
    with client() as api:
        instance = api.post("/api/v1/instances", json=create_payload()).json()
        response = api.get(f"/api/v1/instances/{instance['id']}/strategy-monitor")

    assert response.status_code == 200
    payload = response.json()
    assert payload["instanceId"] == instance["id"]
    assert payload["status"] == "idle"
    assert payload["timeline"] == []
    assert "secret-never-return" not in response.text
    assert "pass-never-return" not in response.text
    assert "proxy-password" not in response.text


def test_beta_source_settings_update_runtime_without_storing_endpoint_credentials() -> None:
    class NoNetworkProvider:
        last_refresh_error = None

        async def refresh(self) -> bool:
            return True

        def seconds_until_refresh(self, maximum_seconds: float) -> float:
            return maximum_seconds

        async def aclose(self) -> None:
            return None

    app = create_app(ControlPlaneSettings(seed_demo_data=False))
    runtime = app.state.beta_source_runtime
    runtime._provider = NoNetworkProvider()  # type: ignore[attr-defined]
    runtime._provider_factory = lambda _settings: NoNetworkProvider()  # type: ignore[attr-defined]
    with TestClient(app) as api:
        current = api.get("/api/v1/beta/source")
        assert current.status_code == 200
        assert current.json()["url"] == "http://127.0.0.1:5888/api/v1/hedge-ratio"

        rejected = api.patch(
            "/api/v1/beta/source",
            json={
                "url": "https://user:password@beta.example.test/ratio",
                "timeoutSeconds": 2,
                "refreshIntervalSeconds": 5,
                "backgroundRefreshEnabled": True,
            },
        )
        assert rejected.status_code == 422

        updated = api.patch(
            "/api/v1/beta/source",
            json={
                "url": "https://beta.example.test/api/v1/ratio",
                "timeoutSeconds": 2.5,
                "refreshIntervalSeconds": 5,
                "backgroundRefreshEnabled": True,
            },
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["url"] == "https://beta.example.test/api/v1/ratio"
        assert updated.json()["timeoutSeconds"] == 2.5
        assert api.get("/api/v1/beta/source").json()["url"] == "https://beta.example.test/api/v1/ratio"


def test_bound_strategy_live_preview_is_read_only_and_confirmation_gated(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from weex_cli.config import Credentials, Settings
    from weex_cli.live_profile import LiveProfile

    settings = ControlPlaneSettings(
        adapter="weex-live",
        storage="sqlite",
        sqlite_path=tmp_path / "fleet.sqlite3",
        master_key=SecretStr(Fernet.generate_key().decode("ascii")),
        seed_demo_data=False,
        live_campaigns_enabled=True,
        live_trading_enabled=True,
        campaign_data_directory=tmp_path / "campaigns",
    )
    profile = LiveProfile(
        path=tmp_path / "profile.toml",
        settings=Settings(
            credentials=Credentials("key", "secret", "pass"), default_mode="live", live_trading_enabled=True
        ),
        proxy_url=None,
        allow_live_mutations=True,
        post_only_only=True,
    )
    monkeypatch.setattr(main_module, "LiveCampaignBetaAllocationProvider", LivePreviewProvider)
    monkeypatch.setattr(
        campaigns_module.CampaignWorkerManager,
        "_profile_and_gateway",
        lambda _self, _material: (profile, LivePreviewGateway()),
    )
    app = create_app(settings)
    with TestClient(app) as api:
        health = api.get("/api/v1/health").json()
        assert health["boundStrategyExecutionEnabled"] is True
        strategy = api.post("/api/v1/strategies", json=strategy_payload(target="1250")).json()
        payload = create_payload(mode="live")
        payload["strategyId"] = strategy["id"]
        created = api.post("/api/v1/instances", json=payload)
        assert created.status_code == 201
        instance = created.json()
        preview = api.post(f"/api/v1/instances/{instance['id']}/strategy-executions/preview", json={})
        assert preview.status_code == 200, preview.text
        body = preview.json()
        assert body["strategyId"] == strategy["id"]
        assert body["strategyVersion"] == 1
        assert body["targetMode"] == "incremental"
        assert body["runDisposition"] == "new_incremental"
        assert body["strategyTargetQuoteVolume"] == "1250"
        assert body["executionTargetQuoteVolume"] == "1250"
        assert body["roundTurnoverQuoteMin"] == "500"
        assert body["cycleVolume"] == "750"
        assert "STRATEGY" in body["confirmation"]
        changed = strategy_payload(name="Changed after preview", target="1250")
        updated = api.patch(f"/api/v1/strategies/{strategy['id']}", json=changed)
        assert updated.status_code == 200, updated.text
        assert updated.json()["version"] == 2
        projection = api.get(f"/api/v1/instances/{instance['id']}")
        assert projection.status_code == 200
        assert projection.json()["strategy"]["name"] == "Changed after preview"
        assert projection.json()["strategy"]["version"] == 2
        executions = api.get(f"/api/v1/instances/{instance['id']}/strategy-executions")
        assert executions.status_code == 200
        assert executions.json()[0]["status"] == "stopped"
        assert executions.json()[0]["reason"] == "shared_strategy_updated"
        invalidation_events = api.get(
            f"/api/v1/instances/{instance['id']}/strategy-executions/{body['campaignId']}/events"
        )
        assert invalidation_events.status_code == 200
        assert invalidation_events.json()[-1]["name"] == "bound_strategy_preview_invalidated"
        refreshed = api.post(f"/api/v1/instances/{instance['id']}/strategy-executions/preview", json={})
        assert refreshed.status_code == 200, refreshed.text
        assert refreshed.json()["campaignId"] != body["campaignId"]
        assert refreshed.json()["strategyVersion"] == 2
        assert refreshed.json()["strategyName"] == "Changed after preview"
        stale = api.post(
            f"/api/v1/instances/{instance['id']}/strategy-executions/{body['campaignId']}/execute",
            json={"riskAcknowledged": True, "confirmation": body["confirmation"]},
        )
        assert stale.status_code == 409
        assert "changed since preview" in stale.json()["detail"]
        assert (
            api.post(
                f"/api/v1/instances/{instance['id']}/strategy-executions/{body['campaignId']}/execute",
                json={"riskAcknowledged": False, "confirmation": body["confirmation"]},
            ).status_code
            == 409
        )
        assert api.get("/api/v1/health").json()["liveCampaignActiveWorkerCount"] == 0


def test_bound_strategy_preview_returns_503_when_final_beta_source_is_unavailable(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from weex_cli.config import Credentials, Settings
    from weex_cli.live_profile import LiveProfile

    settings = ControlPlaneSettings(
        adapter="weex-live",
        storage="sqlite",
        sqlite_path=tmp_path / "fleet.sqlite3",
        master_key=SecretStr(Fernet.generate_key().decode("ascii")),
        seed_demo_data=False,
        live_campaigns_enabled=True,
        live_trading_enabled=True,
        campaign_data_directory=tmp_path / "campaigns",
    )
    profile = LiveProfile(
        path=tmp_path / "profile.toml",
        settings=Settings(
            credentials=Credentials("key", "secret", "pass"), default_mode="live", live_trading_enabled=True
        ),
        proxy_url=None,
        allow_live_mutations=True,
        post_only_only=True,
    )
    monkeypatch.setattr(main_module, "LiveCampaignBetaAllocationProvider", UnavailableLivePreviewProvider)
    monkeypatch.setattr(
        campaigns_module.CampaignWorkerManager,
        "_profile_and_gateway",
        lambda _self, _material: (profile, LivePreviewGateway()),
    )

    with TestClient(create_app(settings)) as api:
        strategy = api.post("/api/v1/strategies", json=strategy_payload(target="1250")).json()
        payload = create_payload(mode="live")
        payload["strategyId"] = strategy["id"]
        instance = api.post("/api/v1/instances", json=payload).json()
        preview = api.post(f"/api/v1/instances/{instance['id']}/strategy-executions/preview", json={})

    assert preview.status_code == 503
    assert preview.json()["detail"] == "final beta source unavailable: beta_request_failed:httperror"


def test_reassigning_a_shared_strategy_invalidates_old_planned_preview(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from weex_cli.config import Credentials, Settings
    from weex_cli.live_profile import LiveProfile

    settings = ControlPlaneSettings(
        adapter="weex-live",
        storage="sqlite",
        sqlite_path=tmp_path / "fleet.sqlite3",
        master_key=SecretStr(Fernet.generate_key().decode("ascii")),
        seed_demo_data=False,
        live_campaigns_enabled=True,
        live_trading_enabled=True,
        campaign_data_directory=tmp_path / "campaigns",
    )
    profile = LiveProfile(
        path=tmp_path / "profile.toml",
        settings=Settings(
            credentials=Credentials("key", "secret", "pass"), default_mode="live", live_trading_enabled=True
        ),
        proxy_url=None,
        allow_live_mutations=True,
        post_only_only=True,
    )
    monkeypatch.setattr(main_module, "LiveCampaignBetaAllocationProvider", LivePreviewProvider)
    monkeypatch.setattr(
        campaigns_module.CampaignWorkerManager,
        "_profile_and_gateway",
        lambda _self, _material: (profile, LivePreviewGateway()),
    )
    with TestClient(create_app(settings)) as api:
        first = api.post("/api/v1/strategies", json=strategy_payload(name="First", target="1250")).json()
        second = api.post("/api/v1/strategies", json=strategy_payload(name="Second", target="1250")).json()
        payload = create_payload(mode="live")
        payload["strategyId"] = first["id"]
        instance = api.post("/api/v1/instances", json=payload).json()
        old_preview = api.post(f"/api/v1/instances/{instance['id']}/strategy-executions/preview", json={}).json()

        assigned = api.post(
            f"/api/v1/strategies/{second['id']}/assign",
            json={"instanceIds": [instance["id"]]},
        )
        assert assigned.status_code == 200, assigned.text
        old = api.get(f"/api/v1/instances/{instance['id']}/strategy-executions/{old_preview['campaignId']}").json()
        assert old["status"] == "stopped"
        assert old["reason"] == "strategy_binding_changed"

        rebound = api.post(
            f"/api/v1/strategies/{first['id']}/assign",
            json={"instanceIds": [instance["id"]]},
        )
        assert rebound.status_code == 200, rebound.text
        projection = api.get(f"/api/v1/instances/{instance['id']}").json()
        assert projection["strategyId"] == first["id"]
        assert projection["strategy"]["version"] == 1
        current_preview = api.post(f"/api/v1/instances/{instance['id']}/strategy-executions/preview", json={})
        assert current_preview.status_code == 200, current_preview.text
        assert current_preview.json()["campaignId"] != old_preview["campaignId"]
        assert current_preview.json()["strategyId"] == first["id"]


def test_shared_strategy_update_is_rejected_while_bound_execution_is_active(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from weex_cli.config import Credentials, Settings
    from weex_cli.live_profile import LiveProfile

    settings = ControlPlaneSettings(
        adapter="weex-live",
        storage="sqlite",
        sqlite_path=tmp_path / "fleet.sqlite3",
        master_key=SecretStr(Fernet.generate_key().decode("ascii")),
        seed_demo_data=False,
        live_campaigns_enabled=True,
        live_trading_enabled=True,
        campaign_data_directory=tmp_path / "campaigns",
    )
    profile = LiveProfile(
        path=tmp_path / "profile.toml",
        settings=Settings(
            credentials=Credentials("key", "secret", "pass"), default_mode="live", live_trading_enabled=True
        ),
        proxy_url=None,
        allow_live_mutations=True,
        post_only_only=True,
    )
    monkeypatch.setattr(main_module, "LiveCampaignBetaAllocationProvider", LivePreviewProvider)
    monkeypatch.setattr(
        campaigns_module.CampaignWorkerManager,
        "_profile_and_gateway",
        lambda _self, _material: (profile, LivePreviewGateway()),
    )
    app = create_app(settings)
    with TestClient(app) as api:
        strategy = api.post("/api/v1/strategies", json=strategy_payload(target="1250")).json()
        payload = create_payload(mode="live")
        payload["strategyId"] = strategy["id"]
        instance = api.post("/api/v1/instances", json=payload).json()
        preview = api.post(f"/api/v1/instances/{instance['id']}/strategy-executions/preview", json={}).json()
        assert app.state.campaign_journal.claim_execution(preview["campaignId"], started_at_ms=1) is True

        changed = api.patch(
            f"/api/v1/strategies/{strategy['id']}",
            json=strategy_payload(name="must not apply", target="1250"),
        )
        assert changed.status_code == 409
        assert "active" in changed.json()["detail"]
        assert api.get("/api/v1/strategies").json()[0]["name"] != "must not apply"


def test_bound_strategy_execution_creates_session_only_after_confirmed_idempotent_claim(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from weex_cli.config import Credentials, Settings
    from weex_cli.live_profile import LiveProfile

    settings = ControlPlaneSettings(
        adapter="weex-live",
        storage="sqlite",
        sqlite_path=tmp_path / "fleet.sqlite3",
        master_key=SecretStr(Fernet.generate_key().decode("ascii")),
        seed_demo_data=False,
        live_campaigns_enabled=True,
        live_trading_enabled=True,
        campaign_data_directory=tmp_path / "campaigns",
    )
    profile = LiveProfile(
        path=tmp_path / "profile.toml",
        settings=Settings(
            credentials=Credentials("key", "secret", "pass"), default_mode="live", live_trading_enabled=True
        ),
        proxy_url=None,
        allow_live_mutations=True,
        post_only_only=True,
    )
    monkeypatch.setattr(main_module, "LiveCampaignBetaAllocationProvider", LivePreviewProvider)
    monkeypatch.setattr(
        campaigns_module.CampaignWorkerManager,
        "_profile_and_gateway",
        lambda _self, _material: (profile, LivePreviewGateway()),
    )
    app = create_app(settings, require_command_id=True)
    held_executor = HeldWorkerExecutor()
    app.state.campaign_manager._executor = held_executor
    with TestClient(app) as api:
        strategy = api.post(
            "/api/v1/strategies",
            json=strategy_payload(target="1250"),
            headers={"X-Fleet-Command-Id": "strategy-create"},
        ).json()
        payload = create_payload(mode="live")
        payload["strategyId"] = strategy["id"]
        instance = api.post(
            "/api/v1/instances",
            json=payload,
            headers={"X-Fleet-Command-Id": "account-create"},
        ).json()
        preview = api.post(
            f"/api/v1/instances/{instance['id']}/strategy-executions/preview",
            json={},
            headers={"X-Fleet-Command-Id": "bound-preview"},
        ).json()
        assert app.state.trade_volume_ledger.latest_session(instance["id"], "live") is None
        assert api.get("/api/v1/health").json()["liveCampaignActiveWorkerCount"] == 0

        headers = {"X-Fleet-Command-Id": "bound-execute-once"}
        execution = api.post(
            f"/api/v1/instances/{instance['id']}/strategy-executions/{preview['campaignId']}/execute",
            json={"riskAcknowledged": True, "confirmation": preview["confirmation"]},
            headers=headers,
        )
        assert execution.status_code == 200, execution.text
        session = app.state.trade_volume_ledger.latest_session(instance["id"], "live")
        assert session is not None
        assert session["target_quote_volume"] == "1250"
        assert held_executor.submissions == 1
        assert api.get("/api/v1/health").json()["liveCampaignActiveWorkerCount"] == 1

        duplicate = api.post(
            f"/api/v1/instances/{instance['id']}/strategy-executions/{preview['campaignId']}/execute",
            json={"riskAcknowledged": True, "confirmation": preview["confirmation"]},
            headers=headers,
        )
        assert duplicate.status_code == 409
        assert held_executor.submissions == 1


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
    payload = response.json()
    assert {
        key: payload[key]
        for key in (
            "status",
            "adapter",
            "storage",
            "liveTradingEnabled",
            "executionEnabled",
            "liveCampaignsEnabled",
            "liveCampaignWorkerCount",
        )
    } == {
        "status": "ok",
        "adapter": "mock",
        "storage": "memory",
        "liveTradingEnabled": False,
        "executionEnabled": True,
        "liveCampaignsEnabled": False,
        "liveCampaignWorkerCount": 0,
    }
    assert payload["apiReleaseId"] == "dev"
    assert payload["executorConnected"] is True
    assert isinstance(payload["executorGeneration"], str)


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
        payload = api.get("/api/v1/health").json()
        assert {
            key: payload[key]
            for key in (
                "status",
                "adapter",
                "storage",
                "liveTradingEnabled",
                "executionEnabled",
                "liveCampaignsEnabled",
                "liveCampaignWorkerCount",
            )
        } == {
            "status": "ok",
            "adapter": "weex-readonly",
            "storage": "memory",
            "liveTradingEnabled": False,
            "executionEnabled": False,
            "liveCampaignsEnabled": False,
            "liveCampaignWorkerCount": 0,
        }
        assert payload["executorConnected"] is True
        assert isinstance(payload["executorGeneration"], str)
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
