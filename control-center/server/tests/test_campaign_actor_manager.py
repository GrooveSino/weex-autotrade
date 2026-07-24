from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from decimal import Decimal

from pydantic import SecretStr
from weex_cli.beta_campaign import live_profile_fingerprint

from fleet_api.campaigns import CampaignWorkerManager, InMemoryCampaignJournal
from fleet_api.models import BetaCampaignStatus
from fleet_api.vault import CredentialMaterial, EphemeralCredentialVault

from .test_campaigns_support import FakeBetaProvider, live_profile, live_settings, metadata, sample_campaign


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
    monkeypatch.setattr("fleet_api.campaign_manager_actor.CampaignActorProgram", _CompletedActorProgram)
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
