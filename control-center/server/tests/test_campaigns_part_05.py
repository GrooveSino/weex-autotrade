from decimal import Decimal

from pydantic import SecretStr

from fleet_api.campaigns import (
    CampaignWorkerManager,
    InMemoryCampaignJournal,
    _sanitize_event,
)
from fleet_api.models import BetaCampaignStatus, VolumeStrategy
from fleet_api.vault import CredentialMaterial, EphemeralCredentialVault

from .test_campaigns_support import (
    FakeBetaProvider,
    FakeGateway,
    live_profile,
    live_settings,
    sample_campaign,
)


def test_stale_planned_bound_strategy_preview_is_invalidated_without_exchange_action(tmp_path) -> None:
    journal = InMemoryCampaignJournal()
    manager = CampaignWorkerManager(
        live_settings(tmp_path),
        EphemeralCredentialVault(),
        journal,
        lambda: FakeBetaProvider(sample_campaign().allocation),  # type: ignore[arg-type]
    )
    profile = live_profile(tmp_path)
    gateway = FakeGateway()
    manager._profile_and_gateway = lambda _material: (profile, gateway)  # type: ignore[method-assign]
    material = CredentialMaterial(
        api_key=SecretStr("key"),
        api_secret=SecretStr("secret"),
        passphrase=SecretStr("passphrase"),
        proxy_url=None,
    )
    initial = VolumeStrategy(
        id="strategy-bound",
        name="Initial",
        target_volume_quote=Decimal("5000"),
        round_turnover_quote_min=Decimal("220"),
        round_turnover_quote_max=Decimal("480"),
        position_hold_min_seconds=7,
        position_hold_max_seconds=9,
        round_interval_min_seconds=11,
        round_interval_max_seconds=13,
    )
    stale = manager.preview_bound_strategy("ins-1", initial, Decimal("1250"), material, session_id="session-1")
    updated = initial.model_copy(
        update={"name": "Updated", "version": 2, "round_turnover_quote_min": Decimal("221")}, deep=True
    )

    assert manager.invalidate_stale_planned_bound_strategy_previews(
        {"ins-1": updated}, reason="executor_startup_strategy_snapshot_stale"
    ) == ["ins-1"]
    record = journal.get(stale.campaign_id)
    assert record is not None
    assert record.status == BetaCampaignStatus.STOPPED.value
    assert record.metadata["invalidation_reason"] == "executor_startup_strategy_snapshot_stale"
    assert record.events[-1]["name"] == "bound_strategy_preview_invalidated"

    current = manager.preview_bound_strategy("ins-1", updated, Decimal("1250"), material, session_id="session-2")
    assert current.campaign_id != stale.campaign_id
    assert current.strategy_version == 2
    manager.close()


def test_dust_close_events_keep_safe_metrics_and_drop_exchange_identifiers() -> None:
    event = _sanitize_event(
        {
            "event": "market_close_verified",
            "symbol": "BTC",
            "action": "close",
            "side": "long",
            "reason": "quote_threshold",
            "quantity": Decimal("0.0001"),
            "quote_volume": Decimal("6.50"),
            "fill_count": 1,
            "verified": True,
            "dust_market_close": True,
            "position_id": "position-secret",
            "order_id": "order-secret",
            "raw_response": {"successOrderId": "order-secret"},
        }
    )

    assert event["fields"] == {
        "symbol": "BTC",
        "action": "close",
        "side": "long",
        "reason": "quote_threshold",
        "quantity": "0.0001",
        "quote_volume": "6.50",
        "fill_count": 1,
        "verified": True,
        "dust_market_close": True,
    }
    assert "position-secret" not in str(event)
    assert "order-secret" not in str(event)
