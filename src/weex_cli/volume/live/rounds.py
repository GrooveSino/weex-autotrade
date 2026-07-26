"""Flat-to-flat round orchestration for live Maker volume sessions."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal

from weex_cli.core.models import decimal_text
from weex_cli.execution.venues import LiveAdaptiveMakerVenue

from .support import is_flat, quantity_for_turnover, round_outcome, safe_position

PositionSide = Literal["long", "short"]


class LiveMakerVolumeRoundsMixin:
    def _execute_round(self, round_number: int, desired_quote: Decimal) -> dict[str, Any]:
        assert self.plan is not None
        position_side: PositionSide = "long" if round_number % 2 else "short"
        venue = self.venue_factory(self.gateway, self.plan.symbol, position_side)
        snapshot = venue.snapshot()
        quantity = quantity_for_turnover(
            self.gateway,
            self.plan.symbol,
            desired_quote,
            Decimal(str(snapshot.mid)),
        )
        opening_notional = quantity * Decimal(str(snapshot.ask if position_side == "long" else snapshot.bid))
        if opening_notional >= self.plan.max_position_quote:
            return round_outcome(round_number, position_side, "stopped", "position_limit_reached", terminal=True)

        open_side = "buy" if position_side == "long" else "sell"
        target = float(quantity) if position_side == "long" else -float(quantity)
        self._emit(
            "volume_round_started",
            round=round_number,
            position_side=position_side,
            desired_quote=decimal_text(desired_quote),
            quantity=decimal_text(quantity),
        )
        opening = self._execute_leg(
            round_number=round_number,
            attempt=1,
            action="open",
            side=open_side,
            target_position=target,
            venue=venue,
            client_prefix=f"{self.plan.plan_id}-r{round_number:03d}-o",
        )
        legs = [opening]
        position = safe_position(venue)
        if opening["execution_uncertain"] or position is None:
            return round_outcome(
                round_number,
                position_side,
                "uncertain",
                opening["reason"],
                legs=legs,
                terminal=True,
                uncertain=True,
            )

        opened = abs(Decimal(str(position))) > self.plan.amount_step / 2
        if opened:
            close_legs, flat, close_reason, close_uncertain = self._flatten_round(
                round_number,
                position_side,
                venue,
            )
            legs.extend(close_legs)
            if not flat:
                return round_outcome(
                    round_number,
                    position_side,
                    "uncertain" if close_uncertain else "stopped",
                    close_reason,
                    legs=legs,
                    terminal=True,
                    uncertain=close_uncertain,
                )

        close_problem = next(
            (
                leg
                for leg in legs[1:]
                if leg["accounting_uncertain"] or leg["taker_or_unknown"] or leg["reason"] == "post_only_rejected"
            ),
            None,
        )
        if close_problem is not None:
            accounting_uncertain = bool(close_problem["accounting_uncertain"])
            return round_outcome(
                round_number,
                position_side,
                "uncertain" if accounting_uncertain else "stopped",
                str(close_problem["reason"]),
                legs=legs,
                terminal=True,
                uncertain=accounting_uncertain,
                flat=True,
            )
        if opening["taker_or_unknown"]:
            return round_outcome(
                round_number,
                position_side,
                "stopped",
                opening["reason"],
                legs=legs,
                terminal=True,
                flat=True,
            )
        if opening["accounting_uncertain"]:
            return round_outcome(
                round_number,
                position_side,
                "uncertain",
                str(opening["reason"]),
                legs=legs,
                terminal=True,
                uncertain=True,
                flat=True,
            )
        if opening["reason"] == "post_only_rejected":
            return round_outcome(
                round_number,
                position_side,
                "stopped",
                "post_only_rejected",
                legs=legs,
                terminal=True,
                flat=not opened or is_flat(venue, self.plan.amount_step),
            )
        if not opened:
            return round_outcome(
                round_number,
                position_side,
                "empty",
                opening["reason"],
                legs=legs,
                terminal=False,
                flat=True,
            )
        if not all(bool(leg["verified_maker"]) for leg in legs if leg["executed_quantity"] != "0"):
            return round_outcome(
                round_number,
                position_side,
                "uncertain",
                "fill_verification_incomplete",
                legs=legs,
                terminal=True,
                uncertain=True,
                flat=True,
            )
        status = "completed" if opening["status"] == "completed" else "recovered"
        return round_outcome(
            round_number,
            position_side,
            status,
            "round_flat" if status == "completed" else "partial_open_recovered_flat",
            legs=legs,
            terminal=False,
            flat=True,
        )

    def _flatten_round(
        self,
        round_number: int,
        position_side: PositionSide,
        venue: LiveAdaptiveMakerVenue,
    ) -> tuple[list[dict[str, Any]], bool, str, bool]:
        assert self.plan is not None
        legs: list[dict[str, Any]] = []
        close_side = "sell" if position_side == "long" else "buy"
        for attempt in range(1, self.plan.recovery_attempts + 1):
            if is_flat(venue, self.plan.amount_step):
                return legs, True, "position_flat", False
            try:
                active = self.gateway.open_orders(self.plan.symbol, mode="live")
            except Exception as exc:  # noqa: BLE001 - never submit while open-order state is unknown
                return legs, False, f"close_preflight:{type(exc).__name__.lower()}", True
            if active:
                return legs, False, "active_order_remains_before_close", True
            leg = self._execute_leg(
                round_number=round_number,
                attempt=attempt,
                action="close",
                side=close_side,
                target_position=0.0,
                venue=venue,
                client_prefix=f"{self.plan.plan_id}-r{round_number:03d}-c{attempt}",
            )
            legs.append(leg)
            if leg["execution_uncertain"]:
                return legs, False, str(leg["reason"]), True
            if leg["accounting_uncertain"]:
                if is_flat(venue, self.plan.amount_step):
                    return legs, True, str(leg["reason"]), True
                continue
            if leg["taker_or_unknown"]:
                if is_flat(venue, self.plan.amount_step):
                    return legs, True, str(leg["reason"]), False
                continue
            if leg["reason"] == "post_only_rejected":
                return legs, is_flat(venue, self.plan.amount_step), "post_only_rejected_during_close", False
            if is_flat(venue, self.plan.amount_step):
                return legs, True, "position_flat", False
        return legs, False, "maker_flatten_attempts_exhausted", False
