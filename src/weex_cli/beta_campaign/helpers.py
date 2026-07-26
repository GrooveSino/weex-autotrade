"""Campaign confirmation, identity, and result-accounting helpers."""

from __future__ import annotations

import hashlib
import shlex
from collections.abc import Mapping
from decimal import Decimal, DecimalException
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from weex_cli.core.errors import SafetyError, ValidationError
from weex_cli.live_profile import LiveProfile

from .model import CAMPAIGN_CONFIRMATION_PATTERN, BetaVolumeCampaign


def campaign_confirmation(campaign: BetaVolumeCampaign) -> str:
    return f"EXECUTE WEEX LIVE BETA-CAMPAIGN {campaign.campaign_id.upper()} RUNS_{campaign.max_runs} POST_ONLY"


def campaign_id_from_confirmation(confirmation: str) -> str:
    match = CAMPAIGN_CONFIRMATION_PATTERN.fullmatch(confirmation)
    if match is None:
        raise ValidationError("invalid Beta campaign confirmation phrase")
    return match.group("campaign_id").lower()


def live_profile_fingerprint(profile: LiveProfile) -> str:
    proxy = urlsplit(profile.proxy_url or "")
    api_key_digest = hashlib.sha256(profile.settings.credentials.api_key.encode("utf-8")).hexdigest()
    identity = "|".join(
        (
            str(profile.path.resolve()),
            api_key_digest,
            proxy.scheme,
            proxy.hostname or "",
            str(proxy.port or ""),
            str(profile.allow_live_mutations),
            str(profile.post_only_only),
        )
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]


def campaign_plan_payload(
    campaign: BetaVolumeCampaign,
    path: Path,
    account_readiness: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "kind": "beta_volume_campaign_plan",
        "status": "dry_run",
        "campaign": campaign.as_dict(),
        "account_readiness": dict(account_readiness),
        "plan_path": str(path),
        "confirm": campaign_confirmation(campaign),
        "timing": {
            "hold_seconds": [campaign.hold_min_seconds, campaign.hold_max_seconds],
            "round_gap_seconds": [campaign.round_gap_min_seconds, campaign.round_gap_max_seconds],
            "selection": "uniform_per_cycle",
        },
        "safety": {
            "single_bounded_authorization": True,
            "post_only": True,
            "authoritative_user_trades_ledger": True,
            "continue_only_from_confirmed_flat_boundary": True,
            "no_automatic_submit_retry_after_uncertainty": True,
            "hard_run_limit": campaign.max_runs,
        },
    }


def campaign_execute_command(campaign: BetaVolumeCampaign, profile_path: Path) -> str:
    phrase = shlex.quote(campaign_confirmation(campaign))
    profile = shlex.quote(str(profile_path))
    return f"WEEX_LIVE_TRADING_ENABLED=true ./weex --profile {profile} live beta-campaign --execute --confirm {phrase}"


def _boundary_is_flat(boundary: Mapping[str, Any]) -> bool:
    keys = ("active_position_count", "regular_order_count", "trigger_order_count")
    try:
        return all(key in boundary and int(boundary[key]) == 0 for key in keys)
    except (TypeError, ValueError):
        return False


def _child_quote(result: Mapping[str, Any]) -> Decimal:
    try:
        quote = Decimal(str(result.get("executed_quote_volume") or 0))
    except (DecimalException, TypeError, ValueError):
        raise SafetyError("child reported invalid quote volume") from None
    if not quote.is_finite() or quote < 0:
        raise SafetyError("child reported invalid quote volume")
    return quote


def _child_is_authoritative(result: Mapping[str, Any]) -> bool:
    accounting = result.get("accounting")
    if not isinstance(accounting, Mapping):
        return False
    return (
        bool(accounting.get("verified"))
        and bool(accounting.get("liquidity_policy_satisfied", accounting.get("maker_only")))
        and int(accounting.get("unknown_liquidity_count") or 0) == 0
    )


def _child_is_pure_maker(result: Mapping[str, Any]) -> bool:
    accounting = result.get("accounting")
    return bool(isinstance(accounting, Mapping) and accounting.get("verified") and accounting.get("maker_only"))


def _authoritative_child_quote(result: Mapping[str, Any]) -> Decimal:
    quote = _child_quote(result)
    if quote == 0:
        return quote
    if not _child_is_authoritative(result):
        raise SafetyError("child volume is not verified userTrades volume under the execution liquidity policy")
    return quote


def _selected_round_turnover(campaign: BetaVolumeCampaign, target: Decimal, run_number: int) -> Decimal:
    """Choose a restart-stable total-turnover amount for one BTC/ETH round.

    The selection is deliberately derived from durable campaign identity and run
    number instead of a process-local RNG. The child plan persists this value
    before any submission; it is never used as executed-volume accounting.
    """
    upper = min(campaign.round_turnover_quote, target)
    lower = min(campaign.round_turnover_quote_min, upper)
    if lower == upper:
        return upper
    cents_low = int((lower * 100).to_integral_value())
    cents_high = int((upper * 100).to_integral_value())
    digest = hashlib.sha256(f"{campaign.campaign_id}:{run_number}".encode()).digest()
    selected = cents_low + (int.from_bytes(digest[:8], "big") % (cents_high - cents_low + 1))
    return Decimal(selected) / Decimal(100)
