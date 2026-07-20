from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal, DecimalException, localcontext
from typing import Any

import httpx

from .execution import AllocationUnavailable, PairAllocation
from .models import BetaMarketSnapshot
from .telemetry import AccountTelemetryContext

_EXPECTED_SCHEMA_VERSION = "1.0"
_EXPECTED_STRATEGY = "btc_long_eth_short"


@dataclass(frozen=True, slots=True)
class _CacheEntry:
    expires_at: float
    allocation: PairAllocation | None = None
    reason_code: str | None = None


@dataclass(frozen=True, slots=True)
class _FetchedAllocation:
    allocation: PairAllocation
    max_cache_seconds: float


class HttpBetaAllocationProvider:
    """Fetches and distributes one validated Beta v2 snapshot to concurrent cycles."""

    def __init__(
        self,
        url: str,
        *,
        timeout_seconds: float,
        cache_seconds: float,
        client: httpx.AsyncClient | None = None,
        network_on_demand: bool = True,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("beta ratio timeout must be greater than 0")
        if cache_seconds <= 0:
            raise ValueError("beta ratio cache duration must be greater than 0")
        self._url = url
        self._timeout_seconds = timeout_seconds
        self._cache_seconds = cache_seconds
        self._client = client or httpx.AsyncClient(trust_env=False, headers={"Accept": "application/json"})
        self._owns_client = client is None
        self._network_on_demand = network_on_demand
        self._lock = asyncio.Lock()
        self._cache: _CacheEntry | None = None
        self._market_snapshot: BetaMarketSnapshot | None = None
        self._market_snapshot_cached_at: float | None = None
        self._last_refresh_error: str | None = None
        self._next_refresh_at: float | None = None

    async def get(self, context: AccountTelemetryContext) -> PairAllocation:
        del context
        cached = self._fresh_cache()
        if cached is not None:
            return self._resolve(cached)
        if not self._network_on_demand:
            raise AllocationUnavailable(self._cached_unavailable_reason())

        async with self._lock:
            cached = self._fresh_cache()
            if cached is not None:
                return self._resolve(cached)
            try:
                fetched = await self._fetch()
            except AllocationUnavailable as exc:
                cached = _CacheEntry(
                    expires_at=time.monotonic() + self._cache_seconds,
                    reason_code=exc.reason_code,
                )
                self._cache = cached
                raise AllocationUnavailable(exc.reason_code) from None
            cached = _CacheEntry(
                expires_at=time.monotonic() + min(self._cache_seconds, fetched.max_cache_seconds),
                allocation=fetched.allocation,
            )
            self._cache = cached
            return fetched.allocation

    async def market_snapshot(self) -> BetaMarketSnapshot:
        if not self._network_on_demand:
            return self._cached_market_snapshot()
        payload, _request_elapsed_seconds = await self._request_payload()
        return self._parse_market_snapshot(payload)

    async def refresh(self) -> bool:
        """Refresh the shared snapshot once; centralized consumers never call the upstream."""
        async with self._lock:
            refresh_started = time.monotonic()
            try:
                payload, request_elapsed_seconds = await self._request_payload()
                market_snapshot = self._parse_market_snapshot(payload)
            except AllocationUnavailable as exc:
                self._last_refresh_error = exc.reason_code
                self._next_refresh_at = refresh_started + self._cache_seconds
                return False

            refreshed_at = time.monotonic()
            self._market_snapshot = market_snapshot
            self._market_snapshot_cached_at = refreshed_at
            try:
                fetched = self._parse_payload(payload, request_elapsed_seconds=request_elapsed_seconds)
            except AllocationUnavailable as exc:
                self._cache = _CacheEntry(
                    expires_at=refreshed_at + self._cache_seconds,
                    reason_code=exc.reason_code,
                )
                self._last_refresh_error = exc.reason_code
                self._next_refresh_at = refreshed_at + self._refresh_before_expiry(self._cache_seconds)
                return False

            fresh_for_seconds = min(self._cache_seconds, fetched.max_cache_seconds)
            self._cache = _CacheEntry(
                expires_at=refreshed_at + fresh_for_seconds,
                allocation=fetched.allocation,
            )
            self._last_refresh_error = None
            self._next_refresh_at = refreshed_at + self._refresh_before_expiry(fresh_for_seconds)
            return True

    def seconds_until_refresh(self, maximum_seconds: float) -> float:
        if maximum_seconds <= 0:
            raise ValueError("maximum refresh interval must be greater than 0")
        next_refresh_at = self._next_refresh_at
        if next_refresh_at is None:
            return maximum_seconds
        return max(0.01, min(maximum_seconds, next_refresh_at - time.monotonic()))

    @property
    def last_refresh_error(self) -> str | None:
        return self._last_refresh_error

    async def aclose(self) -> None:
        self._cache = None
        self._market_snapshot = None
        self._market_snapshot_cached_at = None
        self._last_refresh_error = None
        self._next_refresh_at = None
        if self._owns_client:
            await self._client.aclose()

    def _fresh_cache(self) -> _CacheEntry | None:
        cached = self._cache
        if cached is None or time.monotonic() >= cached.expires_at:
            return None
        return cached

    def _cached_unavailable_reason(self) -> str:
        cached = self._cache
        if cached is None:
            return self._last_refresh_error or "beta_not_ready"
        if cached.reason_code is not None:
            return cached.reason_code
        return self._last_refresh_error or "beta_snapshot_stale"

    def _cached_market_snapshot(self) -> BetaMarketSnapshot:
        snapshot = self._market_snapshot
        cached_at = self._market_snapshot_cached_at
        if snapshot is None or cached_at is None:
            raise AllocationUnavailable(self._last_refresh_error or "beta_not_ready")
        elapsed_ms = Decimal(str(max(0.0, time.monotonic() - cached_at) * 1000))
        return snapshot.model_copy(update={"age_ms": snapshot.age_ms + elapsed_ms})

    @staticmethod
    def _refresh_before_expiry(fresh_for_seconds: float) -> float:
        safety_margin = min(0.5, fresh_for_seconds * 0.1)
        return max(0.01, fresh_for_seconds - safety_margin)

    @staticmethod
    def _resolve(cached: _CacheEntry) -> PairAllocation:
        if cached.allocation is not None:
            return cached.allocation
        assert cached.reason_code is not None
        raise AllocationUnavailable(cached.reason_code)

    async def _fetch(self) -> _FetchedAllocation:
        payload, request_elapsed_seconds = await self._request_payload()
        return self._parse_payload(payload, request_elapsed_seconds=request_elapsed_seconds)

    async def _request_payload(self) -> tuple[dict[str, Any], float]:
        request_started = time.monotonic()
        try:
            response = await self._client.get(self._url, timeout=self._timeout_seconds)
        except httpx.TimeoutException:
            raise AllocationUnavailable("beta_timeout") from None
        except httpx.HTTPError:
            raise AllocationUnavailable("beta_transport") from None
        if response.status_code != 200:
            raise AllocationUnavailable("beta_http_status")
        try:
            payload = response.json()
        except (ValueError, TypeError):
            raise AllocationUnavailable("beta_invalid_json") from None
        if not isinstance(payload, dict):
            raise AllocationUnavailable("beta_invalid_payload")
        return payload, time.monotonic() - request_started

    @classmethod
    def _parse_payload(cls, payload: Any, *, request_elapsed_seconds: float) -> _FetchedAllocation:
        if not isinstance(payload, dict):
            raise AllocationUnavailable("beta_invalid_payload")
        if payload.get("schema_version") != _EXPECTED_SCHEMA_VERSION:
            raise AllocationUnavailable("beta_schema_version")
        if payload.get("strategy") != _EXPECTED_STRATEGY:
            raise AllocationUnavailable("beta_strategy")

        status = payload.get("status")
        if status not in {"ok", "low_confidence"}:
            known_statuses = {"stale", "unavailable"}
            reason = f"beta_status_{status}" if status in known_statuses else "beta_status_invalid"
            raise AllocationUnavailable(reason)

        confidence = cls._decimal_field(payload.get("confidence"), "beta_invalid_confidence")
        confidence_threshold = cls._decimal_field(
            payload.get("confidence_threshold"),
            "beta_invalid_confidence",
        )
        if not Decimal(0) <= confidence <= Decimal(1) or not Decimal(0) <= confidence_threshold <= Decimal(1):
            raise AllocationUnavailable("beta_invalid_confidence")

        age_ms = cls._decimal_field(payload.get("age_ms"), "beta_invalid_age")
        max_age_ms = cls._decimal_field(payload.get("max_age_ms"), "beta_invalid_age")
        if age_ms < 0 or max_age_ms <= 0:
            raise AllocationUnavailable("beta_invalid_age")
        if age_ms > max_age_ms:
            raise AllocationUnavailable("beta_stale_age")
        remaining_freshness_ms = max_age_ms - age_ms - Decimal(str(request_elapsed_seconds * 1000))
        if remaining_freshness_ms <= 0:
            raise AllocationUnavailable("beta_stale_age")

        ratio = payload.get("ratio")
        if not isinstance(ratio, dict):
            raise AllocationUnavailable("beta_invalid_ratio")
        beta = cls._decimal_field(ratio.get("beta"), "beta_invalid_ratio")
        if beta <= 0:
            raise AllocationUnavailable("beta_invalid_ratio")

        as_of = cls._decimal_field(payload.get("as_of"), "beta_invalid_as_of")
        if as_of <= 0:
            raise AllocationUnavailable("beta_invalid_as_of")

        try:
            with localcontext() as context:
                context.prec = 50
                btc_weight = Decimal(1) / (Decimal(1) + beta)
                eth_weight = Decimal(1) - btc_weight
                as_of_ms = int((as_of * 1000).to_integral_value(rounding=ROUND_DOWN))
        except (DecimalException, OverflowError, ValueError):
            raise AllocationUnavailable("beta_invalid_weights") from None
        try:
            return _FetchedAllocation(
                allocation=PairAllocation(
                    btc_weight=btc_weight,
                    eth_weight=eth_weight,
                    version=f"beta-v1:{as_of_ms}",
                ),
                max_cache_seconds=float(remaining_freshness_ms / 1000),
            )
        except ValueError:
            raise AllocationUnavailable("beta_invalid_weights") from None

    @classmethod
    def _parse_market_snapshot(cls, payload: dict[str, Any]) -> BetaMarketSnapshot:
        schema_version = cls._string_field(payload.get("schema_version"), "beta_schema_version")
        strategy = cls._string_field(payload.get("strategy"), "beta_strategy")
        status = cls._string_field(payload.get("status"), "beta_status_invalid")
        source = cls._string_field(payload.get("source"), "beta_invalid_source")
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
        final_beta = cls._decimal_field(ratio.get("beta"), "beta_invalid_ratio")
        btc_long_ratio = cls._decimal_field(ratio.get("btc_long"), "beta_invalid_ratio")
        eth_short_ratio = cls._decimal_field(ratio.get("eth_short"), "beta_invalid_ratio")
        btc_long_weight = cls._decimal_field(allocation.get("btc_long_weight"), "beta_invalid_weights")
        eth_short_weight = cls._decimal_field(allocation.get("eth_short_weight"), "beta_invalid_weights")
        if final_beta <= 0 or btc_long_ratio <= 0 or eth_short_ratio <= 0:
            raise AllocationUnavailable("beta_invalid_ratio")
        if btc_long_weight <= 0 or eth_short_weight <= 0:
            raise AllocationUnavailable("beta_invalid_weights")

        confidence = cls._decimal_field(payload.get("confidence"), "beta_invalid_confidence")
        confidence_threshold = cls._decimal_field(
            payload.get("confidence_threshold"),
            "beta_invalid_confidence",
        )
        as_of = cls._decimal_field(payload.get("as_of"), "beta_invalid_as_of")
        generated_at = cls._decimal_field(payload.get("generated_at"), "beta_invalid_generated_at")
        age_ms = cls._decimal_field(payload.get("age_ms"), "beta_invalid_age")
        max_age_ms = cls._decimal_field(payload.get("max_age_ms"), "beta_invalid_age")
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
            confidence_threshold=confidence_threshold,
            source=source,
            as_of_ms=as_of_ms,
            generated_at_ms=generated_at_ms,
            age_ms=age_ms,
            max_age_ms=max_age_ms,
        )

    @staticmethod
    def _decimal_field(value: Any, reason_code: str) -> Decimal:
        if isinstance(value, bool):
            raise AllocationUnavailable(reason_code)
        try:
            parsed = Decimal(str(value))
        except (DecimalException, TypeError, ValueError):
            raise AllocationUnavailable(reason_code) from None
        if not parsed.is_finite():
            raise AllocationUnavailable(reason_code)
        return parsed

    @staticmethod
    def _string_field(value: Any, reason_code: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise AllocationUnavailable(reason_code)
        return value
