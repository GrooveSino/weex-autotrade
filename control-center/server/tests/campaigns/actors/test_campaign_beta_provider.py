from decimal import Decimal

from weex_cli.beta_campaign.allocation import BetaAllocation

from fleet_api.market.campaign_beta_provider import CachedCampaignBetaProvider
from fleet_api.models import BetaMarketSnapshot


def _snapshot(*, age_ms: str = "100", max_age_ms: str = "1000") -> BetaMarketSnapshot:
    return BetaMarketSnapshot(
        schema_version="1.0",
        strategy="btc_long_eth_short",
        status="ok",
        upstream_usable=True,
        reason_codes=[],
        final_beta=Decimal("0.4"),
        btc_long_ratio=Decimal("1"),
        eth_short_ratio=Decimal("0.4"),
        btc_long_weight=Decimal("0.7142857142857142857142857143"),
        eth_short_weight=Decimal("0.2857142857142857142857142857"),
        confidence=Decimal("0.9"),
        confidence_threshold=Decimal("0.7"),
        source="cached-final-beta",
        as_of_ms=123_000,
        generated_at_ms=124_000,
        age_ms=Decimal(age_ms),
        max_age_ms=Decimal(max_age_ms),
    )


class RuntimeStub:
    def __init__(self, snapshot: BetaMarketSnapshot) -> None:
        self.snapshot = snapshot

    def cached_market_snapshot(self) -> BetaMarketSnapshot:
        return self.snapshot


class FallbackStub:
    def __init__(self) -> None:
        self.calls = 0
        self.allocation = BetaAllocation(
            beta=Decimal("0.5"),
            btc_long_weight=Decimal("0.6666666666666666666666666667"),
            eth_short_weight=Decimal("0.3333333333333333333333333333"),
            version="fallback",
            as_of_ms=1,
            confidence=Decimal("1"),
            confidence_threshold=Decimal("0"),
            source="fallback",
        )

    def get(self) -> BetaAllocation:
        self.calls += 1
        return self.allocation


def test_campaign_beta_provider_uses_shared_fresh_snapshot_without_fallback() -> None:
    fallback = FallbackStub()
    provider = CachedCampaignBetaProvider(RuntimeStub(_snapshot()), fallback)  # type: ignore[arg-type]

    allocation = provider.get()

    assert allocation.beta == Decimal("0.4")
    assert allocation.version == "beta-v1:123000"
    assert allocation.source == "cached-final-beta"
    assert fallback.calls == 0


def test_campaign_beta_provider_derives_exact_weights_from_rounded_snapshot() -> None:
    fallback = FallbackStub()
    snapshot = _snapshot().model_copy(
        update={
            "btc_long_weight": Decimal("0.6630961365388749"),
            "eth_short_weight": Decimal("0.3369038634611250"),
            "final_beta": Decimal("0.5080769512843839"),
        }
    )
    provider = CachedCampaignBetaProvider(RuntimeStub(snapshot), fallback)  # type: ignore[arg-type]

    allocation = provider.get()

    assert allocation.btc_long_weight + allocation.eth_short_weight == Decimal(1)
    assert allocation.beta == snapshot.final_beta
    assert fallback.calls == 0


def test_campaign_beta_provider_falls_back_when_shared_snapshot_is_stale() -> None:
    fallback = FallbackStub()
    provider = CachedCampaignBetaProvider(
        RuntimeStub(_snapshot(age_ms="1000", max_age_ms="1000")),  # type: ignore[arg-type]
        fallback,
    )

    assert provider.get() is fallback.allocation
    assert fallback.calls == 1


def test_campaign_beta_provider_rejects_a_non_executable_cached_status() -> None:
    fallback = FallbackStub()
    snapshot = _snapshot().model_copy(update={"status": "stale", "upstream_usable": False})
    provider = CachedCampaignBetaProvider(RuntimeStub(snapshot), fallback)  # type: ignore[arg-type]

    assert provider.get() is fallback.allocation
    assert fallback.calls == 1


def test_campaign_beta_provider_rejects_snapshot_marked_unusable() -> None:
    fallback = FallbackStub()
    snapshot = _snapshot().model_copy(update={"upstream_usable": False})
    provider = CachedCampaignBetaProvider(RuntimeStub(snapshot), fallback)  # type: ignore[arg-type]

    assert provider.get() is fallback.allocation
    assert fallback.calls == 1
