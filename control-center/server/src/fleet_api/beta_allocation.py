from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from typing import Any

import httpx

from .beta_payload import CacheEntry, FetchedAllocation, parse_allocation, parse_market_snapshot
from .execution import AllocationUnavailable, PairAllocation
from .models import BetaMarketSnapshot
from .telemetry import AccountTelemetryContext

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
        self._cache: CacheEntry | None = None
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
                cached = CacheEntry(
                    expires_at=time.monotonic() + self._cache_seconds,
                    reason_code=exc.reason_code,
                )
                self._cache = cached
                raise AllocationUnavailable(exc.reason_code) from None
            cached = CacheEntry(
                expires_at=time.monotonic() + min(self._cache_seconds, fetched.max_cache_seconds),
                allocation=fetched.allocation,
            )
            self._cache = cached
            return fetched.allocation

    async def market_snapshot(self) -> BetaMarketSnapshot:
        if not self._network_on_demand:
            return self._cached_market_snapshot()
        payload, _request_elapsed_seconds = await self._request_payload()
        return parse_market_snapshot(payload)

    async def refresh(self) -> bool:
        """Refresh the shared snapshot once; centralized consumers never call the upstream."""
        async with self._lock:
            refresh_started = time.monotonic()
            try:
                payload, request_elapsed_seconds = await self._request_payload()
                market_snapshot = parse_market_snapshot(payload)
            except AllocationUnavailable as exc:
                self._last_refresh_error = exc.reason_code
                self._next_refresh_at = refresh_started + self._cache_seconds
                return False

            refreshed_at = time.monotonic()
            self._market_snapshot = market_snapshot
            self._market_snapshot_cached_at = refreshed_at
            try:
                fetched = parse_allocation(payload, request_elapsed_seconds=request_elapsed_seconds)
            except AllocationUnavailable as exc:
                self._cache = CacheEntry(
                    expires_at=refreshed_at + self._cache_seconds,
                    reason_code=exc.reason_code,
                )
                self._last_refresh_error = exc.reason_code
                self._next_refresh_at = refreshed_at + self._refresh_before_expiry(self._cache_seconds)
                return False

            fresh_for_seconds = min(self._cache_seconds, fetched.max_cache_seconds)
            self._cache = CacheEntry(
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

    def _fresh_cache(self) -> CacheEntry | None:
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
    def _resolve(cached: CacheEntry) -> PairAllocation:
        if cached.allocation is not None:
            return cached.allocation
        assert cached.reason_code is not None
        raise AllocationUnavailable(cached.reason_code)

    async def _fetch(self) -> FetchedAllocation:
        payload, request_elapsed_seconds = await self._request_payload()
        return parse_allocation(payload, request_elapsed_seconds=request_elapsed_seconds)

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

    @staticmethod
    def _parse_payload(payload: Any, *, request_elapsed_seconds: float) -> FetchedAllocation:
        return parse_allocation(payload, request_elapsed_seconds=request_elapsed_seconds)

    @staticmethod
    def _parse_market_snapshot(payload: dict[str, Any]) -> BetaMarketSnapshot:
        return parse_market_snapshot(payload)
