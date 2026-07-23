import time
from dataclasses import replace
from decimal import Decimal

import pytest
from pydantic import SecretStr
from weex_cli.beta_campaign import (
    live_profile_fingerprint,
)

from fleet_api.campaign_log import campaign_event_log
from fleet_api.campaigns import (
    CampaignWorkerManager,
    InMemoryCampaignJournal,
)
from fleet_api.models import BetaCampaignStatus
from fleet_api.service import UnsafeOperation
from fleet_api.vault import CredentialMaterial, EphemeralCredentialVault

from .test_campaigns_support import (
    FakeBetaProvider,
    FakeGateway,
    live_profile,
    live_settings,
    metadata,
    sample_campaign,
)


def test_worker_uses_independent_lane_gateways_and_records_events(monkeypatch, tmp_path) -> None:
    allocation = sample_campaign().allocation
    gateway = FakeGateway()
    progress_events: list[tuple[str, dict[str, object]]] = []
    manager = CampaignWorkerManager(
        live_settings(tmp_path),
        EphemeralCredentialVault(),
        InMemoryCampaignJournal(),
        lambda: FakeBetaProvider(allocation),  # type: ignore[arg-type]
        on_progress=lambda instance_id, event: progress_events.append((instance_id, dict(event))),
    )
    profile = live_profile(tmp_path)
    manager._profile_and_gateway = lambda _material: (profile, gateway)  # type: ignore[method-assign]

    class FakeWebSocket:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def start(self) -> None:
            return None

        def close(self) -> None:
            return None

    captured: dict[str, object] = {}

    class FakeCampaignService:
        def __init__(self, primary, _provider, _campaign_store, _child_store, **kwargs) -> None:
            captured["primary"] = primary
            captured["lanes"] = kwargs["lane_gateways"]
            self.event_sink = kwargs["event_sink"]

        def execute(self, _campaign):
            self.event_sink({"event": "campaign_run_started", "run": 1})
            captured["primary"].available = "999.75"
            return {
                "status": "completed",
                "executed_quote_volume": "500",
                "remaining_quote": "0",
                "excess_quote": "0",
                "maker_only": True,
            }

    monkeypatch.setattr("fleet_api.campaigns.WeexCampaignWebSocketRuntime", FakeWebSocket)
    monkeypatch.setattr("fleet_api.campaigns.LiveBetaVolumeCampaignService", FakeCampaignService)
    material = CredentialMaterial(
        api_key=SecretStr("key"),
        api_secret=SecretStr("secret"),
        passphrase=SecretStr("passphrase"),
        proxy_url=SecretStr("proxy:443:user:password"),
    )
    profile = live_profile(tmp_path)
    now_ms = int(time.time() * 1000)
    campaign = replace(
        sample_campaign(),
        created_at_ms=now_ms,
        expires_at_ms=now_ms + 3_600_000,
        profile_fingerprint=live_profile_fingerprint(profile),
    )._with_computed_id()
    manager.journal.create("ins-1", campaign, metadata(campaign))
    manager._verify_execution_boundary = lambda _record, _material: Decimal("1000")  # type: ignore[method-assign]
    with pytest.raises(UnsafeOperation, match="risk acknowledgement"):
        manager.start("ins-1", campaign.campaign_id, str(metadata(campaign)["confirmation"]), False, material)
    with pytest.raises(UnsafeOperation, match="exact campaign confirmation"):
        manager.start("ins-1", campaign.campaign_id, "wrong", True, material)
    manager.start("ins-1", campaign.campaign_id, str(metadata(campaign)["confirmation"]), True, material)
    manager._futures[campaign.campaign_id].result(timeout=3)
    record = manager.journal.get(campaign.campaign_id)
    assert record is not None
    assert record.status == BetaCampaignStatus.COMPLETED.value
    assert record.metadata["current_run"] == 1
    assert record.metadata["starting_available_balance_quote"] == "1000"
    assert record.metadata["ending_available_balance_quote"] == "999.75"
    assert [event["sequence"] for event in record.events] == [1]
    assert progress_events == [("ins-1", record.events[0])]
    lanes = captured["lanes"]
    assert isinstance(lanes, dict)
    assert lanes["BTC"] is not lanes["ETH"]
    assert lanes["BTC"] is not captured["primary"]
    manager.close()

def test_campaign_progress_formatter_is_safe_and_keeps_verified_fill_context() -> None:
    level, message = campaign_event_log(
        {
            "name": "leg_completed",
            "fields": {
                "symbol": "BTCUSDT",
                "action": "open",
                "quote_volume": "250.50",
                "fill_count": 2,
                "api_secret": "must-not-render",
            },
        }
    )

    assert level.value == "success"
    assert message == "实盘执行：BTCUSDT open 成交已核验；250.50 USDT / 2 笔"
    assert "must-not-render" not in message

def test_progress_and_end_balance_failures_do_not_change_worker_result(monkeypatch, tmp_path) -> None:
    allocation = sample_campaign().allocation

    class EndingBalanceFailureGateway(FakeGateway):
        balance_reads = 0

        def account_balance_rows(self, _mode: str) -> list[dict[str, str]]:
            self.balance_reads += 1
            raise TimeoutError("fake balance timeout")

    gateway = EndingBalanceFailureGateway()
    manager = CampaignWorkerManager(
        live_settings(tmp_path),
        EphemeralCredentialVault(),
        InMemoryCampaignJournal(),
        lambda: FakeBetaProvider(allocation),  # type: ignore[arg-type]
        on_progress=lambda _instance_id, _event: (_ for _ in ()).throw(RuntimeError("log unavailable")),
    )
    profile = live_profile(tmp_path)
    manager._profile_and_gateway = lambda _material: (profile, gateway)  # type: ignore[method-assign]

    class FakeWebSocket:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def start(self) -> None:
            return None

        def close(self) -> None:
            return None

    class FakeCampaignService:
        def __init__(self, *_args, **kwargs) -> None:
            self.event_sink = kwargs["event_sink"]

        def execute(self, _campaign):
            self.event_sink({"event": "campaign_run_started", "run": 1, "remaining_quote": "500"})
            return {"status": "completed", "executed_quote_volume": "500", "remaining_quote": "0", "excess_quote": "0"}

    monkeypatch.setattr("fleet_api.campaigns.WeexCampaignWebSocketRuntime", FakeWebSocket)
    monkeypatch.setattr("fleet_api.campaigns.LiveBetaVolumeCampaignService", FakeCampaignService)
    material = CredentialMaterial(
        api_key=SecretStr("key"),
        api_secret=SecretStr("secret"),
        passphrase=SecretStr("passphrase"),
        proxy_url=None,
    )
    now_ms = int(time.time() * 1000)
    campaign = replace(
        sample_campaign(),
        created_at_ms=now_ms,
        expires_at_ms=now_ms + 3_600_000,
        profile_fingerprint=live_profile_fingerprint(profile),
    )._with_computed_id()
    manager.journal.create("ins-1", campaign, metadata(campaign))
    manager._verify_execution_boundary = lambda _record, _material: Decimal("1000")  # type: ignore[method-assign]
    manager.start("ins-1", campaign.campaign_id, str(metadata(campaign)["confirmation"]), True, material)
    manager._futures[campaign.campaign_id].result(timeout=3)

    record = manager.journal.get(campaign.campaign_id)
    assert record is not None
    assert record.status == BetaCampaignStatus.COMPLETED.value
    assert record.metadata["starting_available_balance_quote"] == "1000"
    assert record.metadata["ending_available_balance_quote"] is None
    assert gateway.balance_reads == 1
    assert [event["name"] for event in record.events] == ["campaign_run_started"]
    manager.close()

def test_worker_initialization_failure_aborts_launch_and_is_immediately_restartable(monkeypatch, tmp_path) -> None:
    manager = CampaignWorkerManager(
        live_settings(tmp_path),
        EphemeralCredentialVault(),
        InMemoryCampaignJournal(),
        lambda: FakeBetaProvider(sample_campaign().allocation),  # type: ignore[arg-type]
    )
    profile = live_profile(tmp_path)
    now_ms = int(time.time() * 1000)
    campaign = replace(
        sample_campaign(),
        created_at_ms=now_ms,
        expires_at_ms=now_ms + 3_600_000,
        profile_fingerprint=live_profile_fingerprint(profile),
    )._with_computed_id()
    manager.journal.create("ins-1", campaign, metadata(campaign))
    manager._verify_execution_boundary = lambda _record, _material: Decimal("1000")  # type: ignore[method-assign]
    manager._profile_and_gateway = lambda _material: (_ for _ in ()).throw(RuntimeError("gateway failed"))  # type: ignore[method-assign]
    material = CredentialMaterial(
        api_key=SecretStr("key"),
        api_secret=SecretStr("secret"),
        passphrase=SecretStr("passphrase"),
        proxy_url=SecretStr("proxy:443:user:password"),
    )
    manager.start("ins-1", campaign.campaign_id, str(metadata(campaign)["confirmation"]), True, material)
    manager._futures[campaign.campaign_id].result(timeout=3)
    record = manager.journal.get(campaign.campaign_id)
    assert record is not None
    assert record.status == BetaCampaignStatus.STOPPED.value
    assert record.metadata["reason"] == "launch_aborted:worker_exception:runtimeerror"
    assert record.events[0]["name"] == "launch_aborted"
    assert record.events[0]["sequence"] == 1
    manager.close()

def test_worker_failure_after_submission_boundary_enters_read_only_recovery(tmp_path) -> None:
    journal = InMemoryCampaignJournal()
    manager = CampaignWorkerManager(
        live_settings(tmp_path),
        EphemeralCredentialVault(),
        journal,
        lambda: FakeBetaProvider(sample_campaign().allocation),  # type: ignore[arg-type]
    )
    profile = live_profile(tmp_path)
    now_ms = int(time.time() * 1000)
    campaign = replace(
        sample_campaign(),
        created_at_ms=now_ms,
        expires_at_ms=now_ms + 3_600_000,
        profile_fingerprint=live_profile_fingerprint(profile),
    )._with_computed_id()
    journal.create("ins-1", campaign, metadata(campaign))
    journal.add_event(campaign.campaign_id, {
        "sequence": 1,
        "name": "leg_progress",
        "at_ms": now_ms,
        "fields": {"progress_event": "order_submission_attempted"},
    })
    manager._verify_execution_boundary = lambda _record, _material: Decimal("1000")  # type: ignore[method-assign]
    manager._profile_and_gateway = lambda _material: (_ for _ in ()).throw(RuntimeError("gateway failed"))  # type: ignore[method-assign]
    material = CredentialMaterial(
        api_key=SecretStr("key"), api_secret=SecretStr("secret"),
        passphrase=SecretStr("passphrase"), proxy_url=None,
    )

    manager.start("ins-1", campaign.campaign_id, str(metadata(campaign)["confirmation"]), True, material)
    manager._futures[campaign.campaign_id].result(timeout=3)

    record = journal.get(campaign.campaign_id)
    assert record is not None
    assert record.status == BetaCampaignStatus.RECOVERING.value
    assert record.metadata["reason"] == "worker_exception:runtimeerror"
    manager.close()

def test_start_rechecks_flat_boundary_before_worker_submission(tmp_path) -> None:
    manager = CampaignWorkerManager(
        live_settings(tmp_path),
        EphemeralCredentialVault(),
        InMemoryCampaignJournal(),
        lambda: FakeBetaProvider(sample_campaign().allocation),  # type: ignore[arg-type]
    )
    profile = live_profile(tmp_path)
    now_ms = int(time.time() * 1000)
    campaign = replace(
        sample_campaign(),
        created_at_ms=now_ms,
        expires_at_ms=now_ms + 3_600_000,
        profile_fingerprint=live_profile_fingerprint(profile),
    )._with_computed_id()
    manager.journal.create("ins-1", campaign, metadata(campaign))
    gateway = FakeGateway(positions=True)
    manager._profile_and_gateway = lambda _material: (profile, gateway)  # type: ignore[method-assign]
    material = CredentialMaterial(
        api_key=SecretStr("key"),
        api_secret=SecretStr("secret"),
        passphrase=SecretStr("passphrase"),
        proxy_url=SecretStr("proxy:443:user:password"),
    )

    with pytest.raises(UnsafeOperation, match="启动条件已变化，请重新确认"):
        manager.start("ins-1", campaign.campaign_id, str(metadata(campaign)["confirmation"]), True, material)

    record = manager.journal.get(campaign.campaign_id)
    assert record is not None
    assert record.status == BetaCampaignStatus.STOPPED.value
    assert record.metadata["reason"] == "launch_aborted:execution_boundary:unsafeoperation"
    assert record.events[-1]["name"] == "launch_aborted"
    assert campaign.campaign_id not in manager._futures
    assert gateway.closed
    manager.close()
