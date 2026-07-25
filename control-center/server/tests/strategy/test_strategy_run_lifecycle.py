import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import SecretStr
from weex_cli.config import Credentials, Settings
from weex_cli.live_profile import LiveProfile

import fleet_api.campaigns as campaigns_module
import fleet_api.main as main_module
from fleet_api.auth.vault import CredentialMaterial, EphemeralCredentialVault
from fleet_api.campaigns import CampaignWorkerManager, InMemoryCampaignJournal
from fleet_api.campaigns.core.campaign_helpers import _cleanup_confirmation
from fleet_api.config.config import ControlPlaneSettings
from fleet_api.main import create_app
from fleet_api.models import BetaCampaignStatus
from fleet_api.services.control.service import UnsafeOperation

from ..support.test_api_support import LivePreviewGateway, LivePreviewProvider, create_payload, strategy_payload
from ..support.test_campaigns_support import FakeBetaProvider, FakeGateway, live_settings, sample_campaign


def _profile(tmp_path) -> LiveProfile:
    return LiveProfile(
        path=tmp_path / "profile.toml",
        settings=Settings(
            credentials=Credentials("key", "secret", "pass"),
            default_mode="live",
            live_trading_enabled=True,
        ),
        proxy_url=None,
        allow_live_mutations=True,
        post_only_only=True,
    )


def _live_settings(tmp_path) -> ControlPlaneSettings:
    return ControlPlaneSettings(
        adapter="weex-live",
        storage="sqlite",
        sqlite_path=tmp_path / "fleet.sqlite3",
        master_key=SecretStr(Fernet.generate_key().decode("ascii")),
        seed_demo_data=False,
        live_campaigns_enabled=True,
        live_trading_enabled=True,
        campaign_data_directory=tmp_path / "campaigns",
    )


def test_reopening_planned_preview_is_local_and_reuses_confirmation(tmp_path, monkeypatch) -> None:
    profile = _profile(tmp_path)
    monkeypatch.setattr(main_module, "LiveCampaignBetaAllocationProvider", LivePreviewProvider)
    monkeypatch.setattr(
        campaigns_module.CampaignWorkerManager,
        "_profile_and_gateway",
        lambda _self, _material: (profile, LivePreviewGateway()),
    )
    app = create_app(_live_settings(tmp_path))
    with TestClient(app) as api:
        strategy = api.post("/api/v1/strategies", json=strategy_payload(target="500")).json()
        payload = create_payload(mode="live")
        payload["strategyId"] = strategy["id"]
        instance = api.post("/api/v1/instances", json=payload).json()
        first = api.post(f"/api/v1/instances/{instance['id']}/strategy-run/prepare", json={}).json()
        campaign_id = first["preview"]["campaignId"]
        app.state.campaign_manager.inspect_bound_strategy_boundary = lambda _material: (_ for _ in ()).throw(
            AssertionError("reopening an immutable preview must not read WEEX")
        )

        second = api.post(f"/api/v1/instances/{instance['id']}/strategy-run/prepare", json={})

        assert second.status_code == 200
        body = second.json()
        assert body["disposition"] == "ready"
        assert body["preview"]["campaignId"] == campaign_id
        assert app.state.campaign_journal.get(campaign_id).status == BetaCampaignStatus.PLANNED.value


def test_initial_prepare_reuses_lifecycle_boundary_for_preview(tmp_path, monkeypatch) -> None:
    profile = _profile(tmp_path)

    class NoDuplicateBoundaryGateway(LivePreviewGateway):
        def account_balance_rows(self, _mode: str):  # type: ignore[no-untyped-def]
            raise AssertionError("preview must reuse the lifecycle boundary snapshot")

        positions = account_balance_rows
        open_orders = account_balance_rows
        algo_orders = account_balance_rows

    monkeypatch.setattr(main_module, "LiveCampaignBetaAllocationProvider", LivePreviewProvider)
    monkeypatch.setattr(
        campaigns_module.CampaignWorkerManager,
        "_profile_and_gateway",
        lambda _self, _material: (profile, NoDuplicateBoundaryGateway()),
    )
    app = create_app(_live_settings(tmp_path))
    with TestClient(app) as api:
        strategy = api.post("/api/v1/strategies", json=strategy_payload(target="500")).json()
        payload = create_payload(mode="live")
        payload["strategyId"] = strategy["id"]
        instance = api.post("/api/v1/instances", json=payload).json()
        reads = 0

        def boundary(_material):  # type: ignore[no-untyped-def]
            nonlocal reads
            reads += 1
            return {
                "flat": True,
                "position_count": 0,
                "regular_order_count": 0,
                "trigger_order_count": 0,
                "available_quote": "1000",
                "blocking_positions": [],
                "checked_at_ms": int(time.time() * 1000),
            }

        app.state.campaign_manager.inspect_bound_strategy_boundary = boundary
        prepared = api.post(f"/api/v1/instances/{instance['id']}/strategy-run/prepare", json={})
        assert prepared.status_code == 200
        assert prepared.json()["disposition"] == "ready"
        assert reads == 1


def test_prepare_uses_strategy_direction_and_reuses_sampled_target_until_run_changes(tmp_path, monkeypatch) -> None:
    profile = _profile(tmp_path)
    monkeypatch.setattr(main_module, "LiveCampaignBetaAllocationProvider", LivePreviewProvider)
    monkeypatch.setattr(
        campaigns_module.CampaignWorkerManager,
        "_profile_and_gateway",
        lambda _self, _material: (profile, LivePreviewGateway()),
    )
    app = create_app(_live_settings(tmp_path))
    with TestClient(app) as api:
        strategy_request = strategy_payload(target="1500")
        strategy_request.update(
            {
                "targetVolumeQuoteMin": "1000.00",
                "targetVolumeQuoteMax": "1500.00",
                "direction": "btc_short_eth_long",
            }
        )
        strategy = api.post("/api/v1/strategies", json=strategy_request).json()
        payload = create_payload(mode="live")
        payload["strategyId"] = strategy["id"]
        instance = api.post("/api/v1/instances", json=payload).json()
        path = f"/api/v1/instances/{instance['id']}/strategy-run/prepare"

        first = api.post(path, json={"direction": "btc_long_eth_short"}).json()["preview"]
        repeated = api.post(path, json={}).json()["preview"]

        assert repeated["campaignId"] == first["campaignId"]
        assert repeated["selectedTargetQuoteVolume"] == first["selectedTargetQuoteVolume"]
        assert repeated["confirmation"] == first["confirmation"]
        assert first["direction"] == "btc_short_eth_long"
        assert "DIRECTION_BTC_SHORT_ETH_LONG" in first["confirmation"]

        app.state.campaign_journal.update(
            first["campaignId"],
            status=BetaCampaignStatus.STOPPED.value,
            finished_at_ms=int(time.time() * 1000),
            reason="test_run_finished",
        )
        next_run = api.post(path, json={"direction": "btc_long_eth_short"}).json()["preview"]
        assert next_run["campaignId"] != first["campaignId"]
        assert next_run["direction"] == "btc_short_eth_long"
        assert 1000 <= float(next_run["selectedTargetQuoteVolume"]) <= 1500


def test_lifetime_prepare_uses_verified_ledger_while_baseline_audit_resumes(tmp_path, monkeypatch) -> None:
    profile = _profile(tmp_path)
    monkeypatch.setattr(main_module, "LiveCampaignBetaAllocationProvider", LivePreviewProvider)
    monkeypatch.setattr(
        campaigns_module.CampaignWorkerManager,
        "_profile_and_gateway",
        lambda _self, _material: (profile, LivePreviewGateway()),
    )
    app = create_app(_live_settings(tmp_path))
    with TestClient(app) as api:
        request = strategy_payload(target="19000")
        request.update(
            {
                "targetMode": "lifetime",
                "targetVolumeQuoteMin": "12000.00",
                "targetVolumeQuoteMax": "19000.00",
                "roundIntervalMinSeconds": 3600,
                "roundIntervalMaxSeconds": 10800,
            }
        )
        strategy = api.post("/api/v1/strategies", json=request).json()
        payload = create_payload(mode="live")
        payload["strategyId"] = strategy["id"]
        instance = api.post("/api/v1/instances", json=payload).json()

        prepared = api.post(f"/api/v1/instances/{instance['id']}/strategy-run/prepare", json={})

        assert prepared.status_code == 200
        preview = prepared.json()["preview"]
        assert 12000 <= float(preview["selectedTargetQuoteVolume"]) <= 19000
        assert preview["selectedTargetQuoteVolume"] == preview["strategyTargetQuoteVolume"]
        assert any("历史基线正在后台核验" in warning for warning in preview["warnings"])
        checkpoint = app.state.trade_volume_ledger.sync_checkpoint(instance["id"], "live") or {}
        assert checkpoint["initial_baseline_state"] == "queued"


def test_cleanup_rejects_concurrent_command_for_same_account(tmp_path, monkeypatch) -> None:
    manager = CampaignWorkerManager(
        live_settings(tmp_path),
        EphemeralCredentialVault(),
        InMemoryCampaignJournal(),
        lambda: FakeBetaProvider(sample_campaign().allocation),  # type: ignore[arg-type]
    )
    material = CredentialMaterial(
        api_key=SecretStr("key"),
        api_secret=SecretStr("secret"),
        passphrase=SecretStr("passphrase"),
        proxy_url=None,
    )
    profile = _profile(tmp_path)
    entered = threading.Event()
    release = threading.Event()

    class BlockingGateway(FakeGateway):
        def __init__(self) -> None:
            super().__init__()
            self.cancel_calls = 0

        def cancel_all_orders(self, _symbol: str, *, mode: str, trigger: bool) -> None:
            assert mode == "live"
            self.cancel_calls += 1
            if self.cancel_calls == 1:
                entered.set()
                assert release.wait(timeout=3)

    gateway = BlockingGateway()
    manager._profile_and_gateway = lambda _material: (profile, gateway)  # type: ignore[method-assign]
    confirmation = _cleanup_confirmation("ins-1")
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            manager.cleanup_bound_strategy,
            "ins-1",
            confirmation,
            material,
        )
        assert entered.wait(timeout=3)
        with pytest.raises(UnsafeOperation, match="正在执行"):
            manager.cleanup_bound_strategy("ins-1", confirmation, material)
        release.set()
        assert first.result(timeout=3)["verified"] is True
        assert gateway.cancel_calls == 4
    manager.close()


def test_startup_cleanup_cancels_orders_but_never_closes_existing_position(tmp_path) -> None:
    manager = CampaignWorkerManager(
        live_settings(tmp_path),
        EphemeralCredentialVault(),
        InMemoryCampaignJournal(),
        lambda: FakeBetaProvider(sample_campaign().allocation),  # type: ignore[arg-type]
    )
    material = CredentialMaterial(
        api_key=SecretStr("key"),
        api_secret=SecretStr("secret"),
        passphrase=SecretStr("passphrase"),
        proxy_url=None,
    )
    profile = _profile(tmp_path)

    class ExistingPositionGateway(FakeGateway):
        def __init__(self) -> None:
            super().__init__(positions=True)
            self.regular = True
            self.trigger = True
            self.cancel_calls: list[tuple[str, bool]] = []

        def positions(self, _mode: str, symbol: str) -> list[dict[str, str]]:
            if symbol != "BTC":
                return []
            return [{"size": "0.001", "side": "long", "notional": "52.00", "id": "private-position-id"}]

        def open_orders(self, _symbol: str, *, mode: str = "live") -> list[dict[str, str]]:
            assert mode == "live"
            return [{"id": "regular"}] if self.regular else []

        def algo_orders(self, _symbol: str) -> list[dict[str, str]]:
            return [{"id": "trigger"}] if self.trigger else []

        def cancel_all_orders(self, symbol: str, *, mode: str, trigger: bool) -> None:
            assert mode == "live"
            self.cancel_calls.append((symbol, trigger))
            if trigger:
                self.trigger = False
            else:
                self.regular = False

        def fork(self):  # type: ignore[no-untyped-def]
            return self

        def close_position_id(self, _symbol: str, _position_id: str) -> None:
            raise AssertionError("startup cleanup must never close an existing position")

    gateway = ExistingPositionGateway()
    manager._profile_and_gateway = lambda _material: (profile, gateway)  # type: ignore[method-assign]

    result = manager.cleanup_bound_strategy(
        "ins-1",
        _cleanup_confirmation("ins-1"),
        material,
    )

    assert result["verified"] is True
    assert result["position_count"] == 1
    assert result["blocking_positions"] == [
        {"symbol": "BTC", "side": "long", "quantity": "0.001", "approximate_quote": "52"}
    ]
    assert gateway.cancel_calls == [("BTC", False), ("BTC", True), ("ETH", False), ("ETH", True)]
    manager.close()


def test_recovery_prepare_returns_retryable_unavailable_when_boundary_read_fails(tmp_path, monkeypatch) -> None:
    profile = _profile(tmp_path)
    monkeypatch.setattr(main_module, "LiveCampaignBetaAllocationProvider", LivePreviewProvider)
    monkeypatch.setattr(
        campaigns_module.CampaignWorkerManager,
        "_profile_and_gateway",
        lambda _self, _material: (profile, LivePreviewGateway()),
    )
    app = create_app(_live_settings(tmp_path))
    with TestClient(app) as api:
        strategy = api.post("/api/v1/strategies", json=strategy_payload(target="500")).json()
        payload = create_payload(mode="live")
        payload["strategyId"] = strategy["id"]
        instance = api.post("/api/v1/instances", json=payload).json()
        prepared = api.post(f"/api/v1/instances/{instance['id']}/strategy-run/prepare", json={}).json()
        app.state.campaign_journal.update(prepared["preview"]["campaignId"], status="recovering")
        app.state.campaign_manager.inspect_bound_strategy_boundary = lambda _material: (_ for _ in ()).throw(
            TimeoutError("read timeout")
        )

        response = api.post(f"/api/v1/instances/{instance['id']}/strategy-run/prepare", json={})

        assert response.status_code == 200
        assert response.json()["disposition"] == "unavailable"
        assert response.json()["reasonCode"] == "boundary_unavailable:timeouterror"
