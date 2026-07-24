"""Bridge the shared Beta runtime cache into immutable Campaign previews."""

from __future__ import annotations

from decimal import DecimalException, localcontext
from typing import Protocol

from weex_cli.beta_allocation import BetaAllocation

from .beta_source import BetaSourceRuntime
from .execution import AllocationUnavailable


class _FallbackProvider(Protocol):
    def get(self) -> BetaAllocation: ...


class CachedCampaignBetaProvider:
    """Prefer the centrally refreshed snapshot and retain safe on-demand fallback."""

    def __init__(self, runtime: BetaSourceRuntime, fallback: _FallbackProvider) -> None:
        self._runtime = runtime
        self._fallback = fallback

    def get(self) -> BetaAllocation:
        try:
            snapshot = self._runtime.cached_market_snapshot()
        except AllocationUnavailable:
            return self._fallback.get()
        if (
            snapshot.schema_version != "1.0"
            or snapshot.strategy != "btc_long_eth_short"
            or snapshot.status not in {"ok", "low_confidence"}
            or not snapshot.upstream_usable
            or snapshot.age_ms >= snapshot.max_age_ms
        ):
            return self._fallback.get()
        try:
            # The UI snapshot preserves upstream display precision. Derive the
            # executable weights from Beta so the pair remains exactly
            # complementary even when the displayed weights were rounded.
            with localcontext() as context:
                context.prec = 50
                btc_weight = 1 / (1 + snapshot.final_beta)
                eth_weight = 1 - btc_weight
            return BetaAllocation(
                beta=snapshot.final_beta,
                btc_long_weight=btc_weight,
                eth_short_weight=eth_weight,
                version=f"beta-v1:{snapshot.as_of_ms}",
                as_of_ms=snapshot.as_of_ms,
                confidence=snapshot.confidence,
                confidence_threshold=snapshot.confidence_threshold,
                source=snapshot.source,
                confidence_override=False,
            )
        except (DecimalException, TypeError, ValueError, ZeroDivisionError):
            # A malformed cached projection must never become an internal 500.
            # The existing provider performs the authoritative validation and
            # returns a typed BetaUnavailable error if the source is unusable.
            return self._fallback.get()
