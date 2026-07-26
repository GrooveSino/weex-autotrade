from __future__ import annotations

import json
import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal, DecimalException, localcontext
from typing import Any
from urllib.request import Request, urlopen

from weex_cli.core.errors import WeexCliError

DEFAULT_BETA_URL = "http://127.0.0.1:5888/api/v1/hedge-ratio"
EXPECTED_SCHEMA_VERSION = "1.0"
EXPECTED_STRATEGY = "btc_long_eth_short"


class BetaUnavailable(WeexCliError):
    """The authoritative Beta snapshot is not safe to use for execution."""


@dataclass(frozen=True)
class BetaAllocation:
    beta: Decimal
    btc_long_weight: Decimal
    eth_short_weight: Decimal
    version: str
    as_of_ms: int
    confidence: Decimal
    confidence_threshold: Decimal
    source: str
    confidence_override: bool = False

    @property
    def confidence_enforced(self) -> bool:
        return False

    def __post_init__(self) -> None:
        values = (self.beta, self.btc_long_weight, self.eth_short_weight, self.confidence)
        if not all(value.is_finite() for value in values):
            raise ValueError("Beta allocation values must be finite")
        if self.beta <= 0 or self.btc_long_weight <= 0 or self.eth_short_weight <= 0:
            raise ValueError("Beta allocation values must be positive")
        if self.btc_long_weight + self.eth_short_weight != Decimal(1):
            raise ValueError("Beta allocation weights must sum to 1")

    def as_dict(self) -> dict[str, str | int]:
        return {
            "beta": _decimal_text(self.beta),
            "btc_long_weight": _decimal_text(self.btc_long_weight),
            "eth_short_weight": _decimal_text(self.eth_short_weight),
            "version": self.version,
            "as_of_ms": self.as_of_ms,
            "confidence": _decimal_text(self.confidence),
            "confidence_threshold": _decimal_text(self.confidence_threshold),
            "source": self.source,
            "confidence_override": self.confidence_override,
            "confidence_enforced": self.confidence_enforced,
        }


BetaFetcher = Callable[[str, float], Any]


class HttpBetaAllocationProvider:
    def __init__(
        self,
        url: str = DEFAULT_BETA_URL,
        *,
        timeout_seconds: float = 5.0,
        fetcher: BetaFetcher | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        allow_low_confidence: bool = False,
    ) -> None:
        if timeout_seconds <= 0 or not math.isfinite(timeout_seconds):
            raise ValueError("Beta timeout must be positive and finite")
        self.url = url
        self.timeout_seconds = timeout_seconds
        self.fetcher = fetcher or _fetch_json
        self.monotonic = monotonic
        self.allow_low_confidence = allow_low_confidence

    def get(self) -> BetaAllocation:
        started = self.monotonic()
        try:
            payload = self.fetcher(self.url, self.timeout_seconds)
        except BetaUnavailable:
            raise
        except Exception as exc:
            raise BetaUnavailable(f"beta_request_failed:{type(exc).__name__.lower()}") from exc
        elapsed = self.monotonic() - started
        return parse_beta_payload(
            payload,
            request_elapsed_seconds=max(0.0, elapsed),
            allow_low_confidence=self.allow_low_confidence,
        )


def parse_beta_payload(
    payload: Any,
    *,
    request_elapsed_seconds: float = 0.0,
    allow_low_confidence: bool = False,
) -> BetaAllocation:
    if not isinstance(payload, Mapping):
        raise BetaUnavailable("beta_invalid_payload")
    if payload.get("schema_version") != EXPECTED_SCHEMA_VERSION:
        raise BetaUnavailable("beta_schema_version")
    if payload.get("strategy") != EXPECTED_STRATEGY:
        raise BetaUnavailable("beta_strategy")

    status = payload.get("status")
    if status not in {"ok", "low_confidence"}:
        known = {"stale", "unavailable"}
        reason = f"beta_status_{status}" if status in known else "beta_status_invalid"
        raise BetaUnavailable(reason)

    confidence = _decimal_field(payload.get("confidence"), "beta_invalid_confidence")
    threshold = _decimal_field(payload.get("confidence_threshold"), "beta_invalid_confidence")
    if not Decimal(0) <= confidence <= Decimal(1) or not Decimal(0) <= threshold <= Decimal(1):
        raise BetaUnavailable("beta_invalid_confidence")

    age_ms = _decimal_field(payload.get("age_ms"), "beta_invalid_age")
    max_age_ms = _decimal_field(payload.get("max_age_ms"), "beta_invalid_age")
    elapsed_ms = Decimal(str(request_elapsed_seconds * 1000))
    if age_ms < 0 or max_age_ms <= 0 or age_ms + elapsed_ms >= max_age_ms:
        raise BetaUnavailable("beta_stale_age")

    ratio = payload.get("ratio")
    if not isinstance(ratio, Mapping):
        raise BetaUnavailable("beta_invalid_ratio")
    beta = _decimal_field(ratio.get("beta"), "beta_invalid_ratio")
    if beta <= 0:
        raise BetaUnavailable("beta_invalid_ratio")
    as_of = _decimal_field(payload.get("as_of"), "beta_invalid_as_of")
    if as_of <= 0:
        raise BetaUnavailable("beta_invalid_as_of")

    try:
        with localcontext() as context:
            context.prec = 50
            btc_weight = Decimal(1) / (Decimal(1) + beta)
            eth_weight = Decimal(1) - btc_weight
            as_of_ms = int((as_of * 1000).to_integral_value(rounding=ROUND_DOWN))
    except (DecimalException, OverflowError, ValueError):
        raise BetaUnavailable("beta_invalid_weights") from None

    try:
        return BetaAllocation(
            beta=beta,
            btc_long_weight=btc_weight,
            eth_short_weight=eth_weight,
            version=f"beta-v1:{as_of_ms}",
            as_of_ms=as_of_ms,
            confidence=confidence,
            confidence_threshold=threshold,
            source=str(payload.get("source") or "unknown")[:80],
            confidence_override=False,
        )
    except ValueError:
        raise BetaUnavailable("beta_invalid_weights") from None


def _fetch_json(url: str, timeout_seconds: float) -> Any:
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "weex-autotrade/0.1"})
    with urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310 - URL is explicit CLI configuration
        return json.load(response)


def _decimal_field(value: Any, reason: str) -> Decimal:
    if isinstance(value, bool):
        raise BetaUnavailable(reason)
    try:
        result = Decimal(str(value))
    except (DecimalException, ValueError, TypeError):
        raise BetaUnavailable(reason) from None
    if not result.is_finite():
        raise BetaUnavailable(reason)
    return result


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text
