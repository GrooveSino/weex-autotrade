from __future__ import annotations

from decimal import Decimal

import pytest

from weex_cli.beta_campaign.allocation import BetaUnavailable, HttpBetaAllocationProvider, parse_beta_payload


def healthy_payload() -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "status": "ok",
        "usable": True,
        "strategy": "btc_long_eth_short",
        "as_of": 1_784_382_577.815,
        "age_ms": 100,
        "max_age_ms": 10_000,
        "ratio": {"beta": "0.5"},
        "confidence": "0.7",
        "confidence_threshold": "0.65",
        "source": "test",
    }


def low_confidence_payload() -> dict[str, object]:
    payload = healthy_payload()
    payload.update(
        {
            "status": "low_confidence",
            "usable": False,
            "confidence": "0.59",
        }
    )
    return payload


def test_beta_weights_are_recomputed_from_authoritative_ratio() -> None:
    payload = healthy_payload()
    payload["allocation"] = {"btc_long_weight": "0.01", "eth_short_weight": "0.99"}

    allocation = parse_beta_payload(payload)

    assert allocation.beta == Decimal("0.5")
    assert allocation.btc_long_weight == Decimal("0.66666666666666666666666666666666666666666666666667")
    assert allocation.eth_short_weight == Decimal("0.33333333333333333333333333333333333333333333333333")
    assert allocation.btc_long_weight + allocation.eth_short_weight == Decimal(1)
    assert allocation.version.startswith("beta-v1:")


@pytest.mark.parametrize(
    ("update", "reason"),
    [
        ({"schema_version": "2.0"}, "beta_schema_version"),
        ({"status": "stale"}, "beta_status_stale"),
        ({"confidence": "invalid"}, "beta_invalid_confidence"),
        ({"age_ms": 10_000}, "beta_stale_age"),
        ({"ratio": {"beta": "0"}}, "beta_invalid_ratio"),
    ],
)
def test_beta_validation_fails_closed(update: dict[str, object], reason: str) -> None:
    payload = healthy_payload()
    payload.update(update)

    with pytest.raises(BetaUnavailable, match=reason):
        parse_beta_payload(payload)


def test_request_elapsed_time_counts_against_freshness() -> None:
    with pytest.raises(BetaUnavailable, match="beta_stale_age"):
        parse_beta_payload(healthy_payload(), request_elapsed_seconds=10)


def test_low_confidence_and_unusable_flags_are_metadata_only() -> None:
    allocation = parse_beta_payload(low_confidence_payload())

    assert allocation.confidence == Decimal("0.59")
    assert allocation.confidence < allocation.confidence_threshold
    assert allocation.confidence_enforced is False
    assert allocation.confidence_override is False


def test_legacy_low_confidence_option_is_a_compatible_noop() -> None:
    provider = HttpBetaAllocationProvider(
        fetcher=lambda url, timeout: low_confidence_payload(),
        monotonic=lambda: 1.0,
        allow_low_confidence=True,
    )

    allocation = provider.get()

    assert allocation.confidence_override is False
    assert allocation.version.startswith("beta-v1:")
    assert allocation.confidence == Decimal("0.59")
    assert allocation.confidence < allocation.confidence_threshold
