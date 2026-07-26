"""Terminal outcome classification for a beta-volume execution."""

from __future__ import annotations

from collections.abc import Mapping

HARD_TERMINAL_REASONS = frozenset(
    {
        "amount_precision_rejected",
        "dust_close_audit_pending",
        "minimum_quantity_rejected",
        "post_only_rejected",
        "taker_fill_detected",
        "unknown_liquidity",
        "venue_did_not_accept_post_only",
        "policy_would_take_liquidity",
        "target_overfilled",
    }
)


def is_hard_terminal(reason: str) -> bool:
    return reason in HARD_TERMINAL_REASONS


def is_uncertain_stop(stop: tuple[str, str]) -> bool:
    return stop[0] in {"submission_uncertain", "accounting_uncertain", "observation_uncertain"}


def terminal_reason(stops: Mapping[str, tuple[str, str]]) -> str | None:
    for symbol in ("BTC", "ETH"):
        stop = stops.get(symbol)
        if stop is not None and is_uncertain_stop(stop):
            return stop[1]
    for symbol in ("BTC", "ETH"):
        stop = stops.get(symbol)
        if stop is not None and is_hard_terminal(stop[1]):
            return stop[1]
    return None
