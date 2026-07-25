from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal, DecimalException, localcontext
from typing import Any

from fleet_api.execution import AllocationUnavailable, PairAllocation
from fleet_api.models import BetaMarketSnapshot

EXPECTED_SCHEMA_VERSION = "1.0"
EXPECTED_STRATEGY = "btc_long_eth_short"


@dataclass(frozen=True, slots=True)
class CacheEntry:
    expires_at: float
    allocation: PairAllocation | None = None
    reason_code: str | None = None


@dataclass(frozen=True, slots=True)
class FetchedAllocation:
    allocation: PairAllocation
    max_cache_seconds: float


def parse_allocation(payload: Any, *, request_elapsed_seconds: float) -> FetchedAllocation:
    if not isinstance(payload, dict):
        raise AllocationUnavailable("beta_invalid_payload")
    if payload.get("schema_version") != EXPECTED_SCHEMA_VERSION:
        raise AllocationUnavailable("beta_schema_version")
    if payload.get("strategy") != EXPECTED_STRATEGY:
        raise AllocationUnavailable("beta_strategy")

    status = payload.get("status")
    if status not in {"ok", "low_confidence"}:
        known_statuses = {"stale", "unavailable"}
        reason = f"beta_status_{status}" if status in known_statuses else "beta_status_invalid"
        raise AllocationUnavailable(reason)

    confidence = decimal_field(payload.get("confidence"), "beta_invalid_confidence")
    threshold = decimal_field(payload.get("confidence_threshold"), "beta_invalid_confidence")
    if not Decimal(0) <= confidence <= Decimal(1) or not Decimal(0) <= threshold <= Decimal(1):
        raise AllocationUnavailable("beta_invalid_confidence")

    age_ms = decimal_field(payload.get("age_ms"), "beta_invalid_age")
    max_age_ms = decimal_field(payload.get("max_age_ms"), "beta_invalid_age")
    if age_ms < 0 or max_age_ms <= 0:
        raise AllocationUnavailable("beta_invalid_age")
    if age_ms > max_age_ms:
        raise AllocationUnavailable("beta_stale_age")
    remaining_ms = max_age_ms - age_ms - Decimal(str(request_elapsed_seconds * 1000))
    if remaining_ms <= 0:
        raise AllocationUnavailable("beta_stale_age")

    ratio = payload.get("ratio")
    if not isinstance(ratio, dict):
        raise AllocationUnavailable("beta_invalid_ratio")
    beta = decimal_field(ratio.get("beta"), "beta_invalid_ratio")
    as_of = decimal_field(payload.get("as_of"), "beta_invalid_as_of")
    if beta <= 0:
        raise AllocationUnavailable("beta_invalid_ratio")
    if as_of <= 0:
        raise AllocationUnavailable("beta_invalid_as_of")

    try:
        with localcontext() as context:
            context.prec = 50
            btc_weight = Decimal(1) / (Decimal(1) + beta)
            eth_weight = Decimal(1) - btc_weight
            as_of_ms = int((as_of * 1000).to_integral_value(rounding=ROUND_DOWN))
        return FetchedAllocation(
            allocation=PairAllocation(
                btc_weight=btc_weight,
                eth_weight=eth_weight,
                version=f"beta-v1:{as_of_ms}",
            ),
            max_cache_seconds=float(remaining_ms / 1000),
        )
    except (DecimalException, OverflowError, ValueError):
        raise AllocationUnavailable("beta_invalid_weights") from None


def parse_market_snapshot(payload: dict[str, Any]) -> BetaMarketSnapshot:
    schema_version = string_field(payload.get("schema_version"), "beta_schema_version")
    strategy = string_field(payload.get("strategy"), "beta_strategy")
    status = string_field(payload.get("status"), "beta_status_invalid")
    source = string_field(payload.get("source"), "beta_invalid_source")
    upstream_usable = payload.get("usable")
    if not isinstance(upstream_usable, bool):
        raise AllocationUnavailable("beta_invalid_usable")
    reason_codes = payload.get("reason_codes")
    if not isinstance(reason_codes, list) or not all(isinstance(item, str) for item in reason_codes):
        raise AllocationUnavailable("beta_invalid_reason_codes")

    ratio = payload.get("ratio")
    allocation = payload.get("allocation")
    if not isinstance(ratio, dict):
        raise AllocationUnavailable("beta_invalid_ratio")
    if not isinstance(allocation, dict):
        raise AllocationUnavailable("beta_invalid_weights")
    final_beta = decimal_field(ratio.get("beta"), "beta_invalid_ratio")
    btc_long_ratio = decimal_field(ratio.get("btc_long"), "beta_invalid_ratio")
    eth_short_ratio = decimal_field(ratio.get("eth_short"), "beta_invalid_ratio")
    btc_long_weight = decimal_field(allocation.get("btc_long_weight"), "beta_invalid_weights")
    eth_short_weight = decimal_field(allocation.get("eth_short_weight"), "beta_invalid_weights")
    if final_beta <= 0 or btc_long_ratio <= 0 or eth_short_ratio <= 0:
        raise AllocationUnavailable("beta_invalid_ratio")
    if btc_long_weight <= 0 or eth_short_weight <= 0:
        raise AllocationUnavailable("beta_invalid_weights")

    confidence = decimal_field(payload.get("confidence"), "beta_invalid_confidence")
    threshold = decimal_field(payload.get("confidence_threshold"), "beta_invalid_confidence")
    as_of = decimal_field(payload.get("as_of"), "beta_invalid_as_of")
    generated_at = decimal_field(payload.get("generated_at"), "beta_invalid_generated_at")
    age_ms = decimal_field(payload.get("age_ms"), "beta_invalid_age")
    max_age_ms = decimal_field(payload.get("max_age_ms"), "beta_invalid_age")
    if as_of <= 0 or generated_at <= 0:
        raise AllocationUnavailable("beta_invalid_timestamp")
    if age_ms < 0 or max_age_ms <= 0:
        raise AllocationUnavailable("beta_invalid_age")
    try:
        as_of_ms = int((as_of * 1000).to_integral_value(rounding=ROUND_DOWN))
        generated_at_ms = int((generated_at * 1000).to_integral_value(rounding=ROUND_DOWN))
    except (DecimalException, OverflowError, ValueError):
        raise AllocationUnavailable("beta_invalid_timestamp") from None

    return BetaMarketSnapshot(
        schema_version=schema_version,
        strategy=strategy,
        status=status,
        upstream_usable=upstream_usable,
        reason_codes=reason_codes,
        final_beta=final_beta,
        btc_long_ratio=btc_long_ratio,
        eth_short_ratio=eth_short_ratio,
        btc_long_weight=btc_long_weight,
        eth_short_weight=eth_short_weight,
        confidence=confidence,
        confidence_threshold=threshold,
        source=source,
        as_of_ms=as_of_ms,
        generated_at_ms=generated_at_ms,
        age_ms=age_ms,
        max_age_ms=max_age_ms,
    )


def decimal_field(value: Any, reason_code: str) -> Decimal:
    if isinstance(value, bool):
        raise AllocationUnavailable(reason_code)
    try:
        parsed = Decimal(str(value))
    except (DecimalException, TypeError, ValueError):
        raise AllocationUnavailable(reason_code) from None
    if not parsed.is_finite():
        raise AllocationUnavailable(reason_code)
    return parsed


def string_field(value: Any, reason_code: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AllocationUnavailable(reason_code)
    return value
