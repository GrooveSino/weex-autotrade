from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from decimal import Decimal

import pytest
from pydantic import SecretStr
from weex_cli.beta_campaign import live_profile_fingerprint

from fleet_api.auth.vault import CredentialMaterial, EphemeralCredentialVault
from fleet_api.campaigns import CampaignWorkerManager, InMemoryCampaignJournal
from fleet_api.models import BetaCampaignStatus
from fleet_api.services.control.service import UnsafeOperation

from ...support.test_campaigns_support import FakeBetaProvider, live_profile, live_settings, metadata, sample_campaign


class _CompletedActorProgram:
    def __init__(self, _campaign, _phases, *, on_result, **_kwargs) -> None:  # type: ignore[no-untyped-def]
        self._on_result = on_result

    async def __call__(self, actor) -> None:  # type: ignore[no-untyped-def]
        actor.transition("preparing")
        await asyncio.sleep(0)
        self._on_result(
            {
                "status": "completed",
                "reason": "target_verified_complete",
                "executed_quote_volume": "25",
                "remaining_quote": "0",
                "excess_quote": "0",
            }
        )
        actor.transition("completed")


class _RehydratedActorProgram:
    resume_contexts: list[object] = []

    def __init__(self, _campaign, _phases, *, resume_context=None, **_kwargs) -> None:  # type: ignore[no-untyped-def]
        self._resume_context = resume_context

    async def __call__(self, actor) -> None:  # type: ignore[no-untyped-def]
        type(self).resume_contexts.append(self._resume_context)
        actor.transition("condition_waiting", reason="beta_unavailable")


def test_manager_admits_and_releases_an_async_actor_without_legacy_worker(monkeypatch, tmp_path) -> None:
    settings = replace(live_settings(tmp_path), async_actor_runtime_enabled=True, max_active_executions=1)
    manager = CampaignWorkerManager(
        settings,
        EphemeralCredentialVault(),
        InMemoryCampaignJournal(),
        lambda: FakeBetaProvider(sample_campaign().allocation),  # type: ignore[arg-type]
    )
    profile = live_profile(tmp_path)
    campaign = replace(
        sample_campaign(),
        created_at_ms=(now_ms := int(time.time() * 1_000)),
        expires_at_ms=now_ms + 3_600_000,
        profile_fingerprint=live_profile_fingerprint(profile),
    )._with_computed_id()
    manager.journal.create("ins-1", campaign, metadata(campaign))
    manager._verify_execution_boundary = lambda _record, _material: Decimal("1000")  # type: ignore[method-assign]
    manager._read_ending_available = lambda _material: "1000"  # type: ignore[method-assign]
    monkeypatch.setattr(
        "fleet_api.campaigns.manager.campaign_manager_actor.CampaignActorProgram", _CompletedActorProgram
    )
    material = CredentialMaterial(
        api_key=SecretStr("key"),
        api_secret=SecretStr("secret"),
        passphrase=SecretStr("passphrase"),
        proxy_url=None,
    )

    view = manager.start("ins-1", campaign.campaign_id, str(metadata(campaign)["confirmation"]), True, material)
    future = manager._actor_futures[campaign.campaign_id]
    future.result(timeout=3)

    record = manager.journal.get(campaign.campaign_id)
    assert view.status == BetaCampaignStatus.EXECUTING
    assert record is not None
    assert record.status == BetaCampaignStatus.COMPLETED
    assert manager.active_worker_count() == 0
    assert manager.capacity_snapshot().active_executions == 0
    assert campaign.campaign_id not in manager._futures
    manager.close()


def test_restart_rehydrates_a_flat_condition_wait_without_recovery_or_reconfirmation(monkeypatch, tmp_path) -> None:
    settings = replace(live_settings(tmp_path), async_actor_runtime_enabled=True, max_active_executions=1)
    vault = EphemeralCredentialVault()
    manager = CampaignWorkerManager(
        settings,
        vault,
        InMemoryCampaignJournal(),
        lambda: FakeBetaProvider(sample_campaign().allocation),  # type: ignore[arg-type]
    )
    campaign = replace(sample_campaign(), expires_at_ms=int(time.time() * 1_000) - 1)._with_computed_id()
    record_metadata = {**metadata(campaign), "phase": "condition_waiting"}
    manager.journal.create("ins-1", campaign, record_metadata)
    manager.journal.update(campaign.campaign_id, status=BetaCampaignStatus.EXECUTING.value)
    vault.put(
        "ins-1",
        CredentialMaterial(
            api_key=SecretStr("key"), api_secret=SecretStr("secret"), passphrase=SecretStr("passphrase"), proxy_url=None
        ),
    )
    _RehydratedActorProgram.resume_contexts.clear()
    monkeypatch.setattr(
        "fleet_api.campaigns.manager.campaign_manager_actor.CampaignActorProgram", _RehydratedActorProgram
    )

    assert manager.recover() == 1
    for _ in range(30):
        if _RehydratedActorProgram.resume_contexts:
            break
        time.sleep(0.01)

    record = manager.journal.get(campaign.campaign_id)
    assert _RehydratedActorProgram.resume_contexts == [None]
    assert record is not None
    assert record.status == BetaCampaignStatus.EXECUTING
    assert record.metadata["phase"] == "condition_waiting"
    manager.close()


def test_failed_session_establishment_publishes_the_stopped_campaign(tmp_path) -> None:
    notifications: list[str] = []

    def fail_claim(_record, _started_at_ms) -> None:  # type: ignore[no-untyped-def]
        raise RuntimeError("ledger unavailable")

    manager = CampaignWorkerManager(
        replace(live_settings(tmp_path), async_actor_runtime_enabled=True),
        EphemeralCredentialVault(),
        InMemoryCampaignJournal(),
        lambda: FakeBetaProvider(sample_campaign().allocation),  # type: ignore[arg-type]
        on_change=notifications.append,
        on_execution_claim=fail_claim,
    )
    profile = live_profile(tmp_path)
    campaign = replace(
        sample_campaign(),
        created_at_ms=(now_ms := int(time.time() * 1_000)),
        expires_at_ms=now_ms + 3_600_000,
        profile_fingerprint=live_profile_fingerprint(profile),
    )._with_computed_id()
    manager.journal.create("ins-1", campaign, metadata(campaign))
    manager._verify_execution_boundary = lambda _record, _material: Decimal("1000")  # type: ignore[method-assign]
    material = CredentialMaterial(
        api_key=SecretStr("key"),
        api_secret=SecretStr("secret"),
        passphrase=SecretStr("passphrase"),
        proxy_url=None,
    )
    try:
        with pytest.raises(UnsafeOperation, match="local ledger session"):
            manager.start("ins-1", campaign.campaign_id, str(metadata(campaign)["confirmation"]), True, material)

        record = manager.journal.get(campaign.campaign_id)
        assert record is not None
        assert record.status == BetaCampaignStatus.STOPPED.value
        assert notifications == ["ins-1"]
    finally:
        manager.close()


def test_recovery_safe_stop_construction_failure_is_chinese_and_releases_capacity(tmp_path) -> None:
    settings = replace(live_settings(tmp_path), async_actor_runtime_enabled=True, max_active_executions=1)
    manager = CampaignWorkerManager(
        settings,
        EphemeralCredentialVault(),
        InMemoryCampaignJournal(),
        lambda: FakeBetaProvider(sample_campaign().allocation),  # type: ignore[arg-type]
    )
    campaign = sample_campaign()
    details = metadata(campaign)
    manager.journal.create("ins-1", campaign, details)
    manager.journal.update(
        campaign.campaign_id,
        status="recovering",
        recovery_boundary_state="owned_exposure",
    )
    material = CredentialMaterial(
        api_key=SecretStr("key"),
        api_secret=SecretStr("secret"),
        passphrase=SecretStr("passphrase"),
        proxy_url=None,
    )
    try:
        with pytest.raises(UnsafeOperation, match="安全收尾无法启动.*未提交新的平仓命令"):
            manager.stop(
                "ins-1",
                campaign.campaign_id,
                str(details["stop_confirmation"]),
                material,
            )
        assert manager.active_worker_count() == 0
        assert manager.capacity_snapshot().active_executions == 0
    finally:
        manager.close()
