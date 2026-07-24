from dataclasses import replace
from decimal import Decimal

import pytest
from pydantic import SecretStr
from weex_cli.beta_campaign import (
    _selected_round_turnover,
    live_profile_fingerprint,
)

from fleet_api.campaigns import (
    CampaignWorkerManager,
    InMemoryCampaignJournal,
)
from fleet_api.config import ControlPlaneSettings
from fleet_api.models import BetaCampaignPreviewRequest, BetaCampaignStatus, StrategyDirection, VolumeStrategy
from fleet_api.service import BetaSourceUnavailable, UnsafeOperation
from fleet_api.vault import CredentialMaterial, EphemeralCredentialVault

from .test_campaigns_support import (
    FakeBetaProvider,
    FakeGateway,
    UnavailableBetaProvider,
    live_profile,
    live_settings,
    metadata,
    sample_campaign,
)


def test_uncertain_campaign_is_recovered_by_read_only_boundary_check_before_preview(tmp_path) -> None:
    manager = CampaignWorkerManager(
        live_settings(tmp_path),
        EphemeralCredentialVault(),
        InMemoryCampaignJournal(),
        lambda: FakeBetaProvider(sample_campaign().allocation),  # type: ignore[arg-type]
    )
    profile = live_profile(tmp_path)
    campaign = replace(
        sample_campaign(),
        profile_fingerprint=live_profile_fingerprint(profile),
    )._with_computed_id()
    manager.journal.create("ins-1", campaign, metadata(campaign))
    manager.journal.update(campaign.campaign_id, status=BetaCampaignStatus.UNCERTAIN.value)
    gateway = FakeGateway()
    manager._profile_and_gateway = lambda _material: (profile, gateway)  # type: ignore[method-assign]
    material = CredentialMaterial(
        api_key=SecretStr("key"),
        api_secret=SecretStr("secret"),
        passphrase=SecretStr("passphrase"),
        proxy_url=SecretStr("proxy:443:user:password"),
    )

    preview = manager.preview(
        "ins-1",
        BetaCampaignPreviewRequest(target_quote=Decimal("6000"), cycle_volume=Decimal("500")),
        material,
    )
    assert preview.campaign_id != campaign.campaign_id
    recovered = manager.get("ins-1", campaign.campaign_id)
    assert recovered.status is BetaCampaignStatus.UNCERTAIN
    assert recovered.reconciliation_required is False
    assert recovered.reconciliation_confirmation is None
    assert recovered.events[-1].name == "campaign_recovery_verified"
    assert gateway.closed
    manager.close()


def test_uncertain_campaign_with_positions_still_blocks_preview_without_mutation(tmp_path) -> None:
    manager = CampaignWorkerManager(
        live_settings(tmp_path),
        EphemeralCredentialVault(),
        InMemoryCampaignJournal(),
        lambda: FakeBetaProvider(sample_campaign().allocation),  # type: ignore[arg-type]
    )
    profile = live_profile(tmp_path)
    campaign = replace(
        sample_campaign(),
        profile_fingerprint=live_profile_fingerprint(profile),
    )._with_computed_id()
    manager.journal.create("ins-1", campaign, metadata(campaign))
    manager.journal.update(campaign.campaign_id, status=BetaCampaignStatus.UNCERTAIN.value)
    gateway = FakeGateway(positions=True)
    manager._profile_and_gateway = lambda _material: (profile, gateway)  # type: ignore[method-assign]
    material = CredentialMaterial(
        api_key=SecretStr("key"),
        api_secret=SecretStr("secret"),
        passphrase=SecretStr("passphrase"),
        proxy_url=SecretStr("proxy:443:user:password"),
    )

    with pytest.raises(UnsafeOperation, match="BTC/ETH 持仓"):
        manager.preview(
            "ins-1",
            BetaCampaignPreviewRequest(target_quote=Decimal("6000"), cycle_volume=Decimal("500")),
            material,
        )

    record = manager.journal.get(campaign.campaign_id)
    assert record is not None
    assert record.metadata.get("reconciliation_acknowledged_at_ms") is None
    assert record.events == ()
    assert gateway.closed
    manager.close()


def test_uncertain_campaign_with_changed_live_profile_cannot_be_auto_recovered(tmp_path) -> None:
    manager = CampaignWorkerManager(
        live_settings(tmp_path),
        EphemeralCredentialVault(),
        InMemoryCampaignJournal(),
        lambda: FakeBetaProvider(sample_campaign().allocation),  # type: ignore[arg-type]
    )
    original_profile = live_profile(tmp_path)
    campaign = replace(
        sample_campaign(),
        profile_fingerprint=live_profile_fingerprint(original_profile),
    )._with_computed_id()
    manager.journal.create("ins-1", campaign, metadata(campaign))
    manager.journal.update(campaign.campaign_id, status=BetaCampaignStatus.UNCERTAIN.value)
    gateway = FakeGateway()
    changed_profile = replace(original_profile, proxy_url="https://changed.example.test:443")
    manager._profile_and_gateway = lambda _material: (changed_profile, gateway)  # type: ignore[method-assign]
    material = CredentialMaterial(
        api_key=SecretStr("key"),
        api_secret=SecretStr("secret"),
        passphrase=SecretStr("passphrase"),
        proxy_url=SecretStr("proxy:443:user:password"),
    )

    with pytest.raises(UnsafeOperation, match="Live profile changed"):
        manager.preview(
            "ins-1",
            BetaCampaignPreviewRequest(target_quote=Decimal("6000"), cycle_volume=Decimal("500")),
            material,
        )

    record = manager.journal.get(campaign.campaign_id)
    assert record is not None
    assert record.metadata.get("reconciliation_acknowledged_at_ms") is None
    assert record.events == ()
    assert gateway.closed
    manager.close()


def test_manager_keeps_live_campaigns_disabled_by_default() -> None:
    settings = ControlPlaneSettings(seed_demo_data=False)
    manager = CampaignWorkerManager(settings, EphemeralCredentialVault(), InMemoryCampaignJournal(), lambda: None)  # type: ignore[arg-type]
    with pytest.raises(UnsafeOperation, match="disabled"):
        manager.preview(
            "ins-1",
            BetaCampaignPreviewRequest(target_quote=Decimal("6000"), cycle_volume=Decimal("500")),
            None,
        )
    manager.close()


def test_campaign_payload_never_contains_credential_material() -> None:
    material = {
        "api_key": SecretStr("key"),
        "api_secret": SecretStr("secret"),
        "passphrase": SecretStr("pass"),
    }
    assert all(value.get_secret_value() not in str(metadata(sample_campaign())) for value in material.values())


def test_bound_strategy_preview_uses_persisted_range_and_read_only_snapshot(tmp_path) -> None:
    manager = CampaignWorkerManager(
        live_settings(tmp_path),
        EphemeralCredentialVault(),
        InMemoryCampaignJournal(),
        lambda: FakeBetaProvider(sample_campaign().allocation),  # type: ignore[arg-type]
    )
    profile = live_profile(tmp_path)
    gateway = FakeGateway()
    manager._profile_and_gateway = lambda _material: (profile, gateway)  # type: ignore[method-assign]
    strategy = VolumeStrategy(
        id="strategy-bound",
        name="Shared Live Range",
        target_volume_quote=Decimal("5000"),
        round_turnover_quote_min=Decimal("220"),
        round_turnover_quote_max=Decimal("480"),
        position_hold_min_seconds=7,
        position_hold_max_seconds=9,
        round_interval_min_seconds=11,
        round_interval_max_seconds=13,
    )
    material = CredentialMaterial(
        api_key=SecretStr("key"),
        api_secret=SecretStr("secret"),
        passphrase=SecretStr("passphrase"),
        proxy_url=None,
    )
    preview = manager.preview_bound_strategy(
        "ins-1",
        strategy,
        Decimal("1250"),
        material,
        session_id="session-bound",
        strategy_target_quote=Decimal("1500"),
        direction=StrategyDirection.BTC_SHORT_ETH_LONG,
    )
    record = manager.journal.get(preview.campaign_id)
    assert record is not None
    assert preview.strategy_id == strategy.id
    assert preview.strategy_name == strategy.name
    assert preview.strategy_version == 1
    assert preview.direction is StrategyDirection.BTC_SHORT_ETH_LONG
    assert preview.selected_target_quote_volume == Decimal("1500")
    assert preview.leverage == 400
    assert preview.margin_mode == "cross"
    assert preview.round_turnover_quote_min == Decimal("220")
    assert preview.cycle_volume == Decimal("480")
    assert record.metadata["session_id"] == "session-bound"
    assert record.metadata["strategy_snapshot"]["roundTurnoverQuoteMin"] == "220"  # type: ignore[index]
    assert record.metadata["strategy_version"] == 1
    selected = _selected_round_turnover(record.campaign, Decimal("1250"), 2)
    assert Decimal("220") <= selected <= Decimal("480")
    assert selected == _selected_round_turnover(record.campaign, Decimal("1250"), 2)
    assert "DIRECTION_BTC_SHORT_ETH_LONG" in preview.confirmation
    assert gateway.balance_reads == 1
    manager.close()


def test_bound_strategy_prepare_returns_the_existing_active_execution_without_another_preflight(tmp_path) -> None:
    manager = CampaignWorkerManager(
        live_settings(tmp_path),
        EphemeralCredentialVault(),
        InMemoryCampaignJournal(),
        lambda: FakeBetaProvider(sample_campaign().allocation),  # type: ignore[arg-type]
    )
    gateway = FakeGateway()
    manager._profile_and_gateway = lambda _material: (live_profile(tmp_path), gateway)  # type: ignore[method-assign]
    strategy = VolumeStrategy(
        id="strategy-1",
        version=1,
        name="Bound",
        target_mode="incremental",
        target_volume_quote=Decimal("1250"),
        round_turnover_quote_min=Decimal("200"),
        round_turnover_quote_max=Decimal("300"),
        position_hold_min_seconds=5,
        position_hold_max_seconds=9,
        round_interval_min_seconds=11,
        round_interval_max_seconds=13,
    )
    material = CredentialMaterial(
        api_key=SecretStr("key"),
        api_secret=SecretStr("secret"),
        passphrase=SecretStr("passphrase"),
        proxy_url=None,
    )

    first = manager.preview_bound_strategy("ins-1", strategy, Decimal("1250"), material, session_id="session-1")
    balance_reads_after_first = gateway.balance_reads
    second = manager.preview_bound_strategy("ins-1", strategy, Decimal("1250"), material, session_id="session-2")

    assert second.campaign_id == first.campaign_id
    assert len(manager.journal.list_for_instance("ins-1")) == 1
    assert gateway.balance_reads == balance_reads_after_first
    manager.close()


def test_bound_strategy_preview_reports_beta_source_unavailable_without_creating_a_campaign(tmp_path) -> None:
    journal = InMemoryCampaignJournal()
    manager = CampaignWorkerManager(
        live_settings(tmp_path),
        EphemeralCredentialVault(),
        journal,
        lambda: UnavailableBetaProvider(),  # type: ignore[arg-type]
    )
    profile = live_profile(tmp_path)
    gateway = FakeGateway()
    manager._profile_and_gateway = lambda _material: (profile, gateway)  # type: ignore[method-assign]
    strategy = VolumeStrategy(
        id="strategy-bound",
        name="Shared Live Range",
        target_volume_quote=Decimal("5000"),
        round_turnover_quote_min=Decimal("220"),
        round_turnover_quote_max=Decimal("480"),
        position_hold_min_seconds=7,
        position_hold_max_seconds=9,
        round_interval_min_seconds=11,
        round_interval_max_seconds=13,
    )
    material = CredentialMaterial(
        api_key=SecretStr("key"),
        api_secret=SecretStr("secret"),
        passphrase=SecretStr("passphrase"),
        proxy_url=None,
    )

    with pytest.raises(BetaSourceUnavailable, match="final beta source unavailable"):
        manager.preview_bound_strategy("ins-1", strategy, Decimal("1250"), material, session_id="session-bound")

    assert journal.list_for_instance("ins-1") == []
    assert gateway.closed is True
    manager.close()
