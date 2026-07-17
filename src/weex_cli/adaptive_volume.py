from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from weex_cli.adaptive_executor import MakerVenue, TargetRequest, execute_adaptive_maker_target
from weex_cli.adaptive_maker import AdaptiveMakerPolicy, MakerPolicyConfig
from weex_cli.demo_maker_venue import MIN_SUBMIT_INTERVAL_SECONDS, DemoAdaptiveMakerVenue
from weex_cli.errors import ValidationError
from weex_cli.gateway import WeexGateway
from weex_cli.maker_volume import VOLUME_BUFFER, MakerVolumePlan
from weex_cli.models import decimal_text

VenueFactory = Callable[[WeexGateway, str], MakerVenue]
VolumeServiceFactory = Callable[[WeexGateway], "AdaptiveMakerVolumeService"]

REAL_POLICY = MakerPolicyConfig(
    min_rest_ms=3000,
    max_rest_ms=20000,
    stale_ticks=13,
    improve_spread_ticks=2,
    min_fill_probability=0.05,
    adverse_threshold=1.0,
    queue_ahead_factor=0.35,
    urgency_weight=0.8,
    child_fraction=1.0,
    passive_guard_ticks=5,
    urgent_guard_ticks=1,
    max_passive_guard_ticks=20,
    volatility_guard_multiplier=2.0,
)


@dataclass(frozen=True)
class MakerSoakPlan:
    volume_plan: MakerVolumePlan
    rounds: int

    def __post_init__(self) -> None:
        if not 2 <= self.rounds <= 10:
            raise ValidationError("Demo Maker soak rounds must be between 2 and 10")

    def as_dict(self) -> dict[str, Any]:
        return {**self.volume_plan.as_dict(), "rounds": self.rounds}


class MakerFlattenService:
    def __init__(self, gateway: WeexGateway, *, venue_factory: VenueFactory = DemoAdaptiveMakerVenue) -> None:
        self.gateway = gateway
        self.venue_factory = venue_factory

    def run(
        self, *, symbol: str, quantity: Decimal, max_position_quote: Decimal, timeout_seconds: int
    ) -> dict[str, Any]:
        if self.gateway.open_orders(None, mode="demo"):
            return {"status": "stopped", "reason": "starting_open_orders_present"}
        venue = self.venue_factory(self.gateway, symbol)
        current = Decimal(str(venue.position_quantity()))
        snapshot = venue.snapshot()
        tolerance = _amount_tolerance(self.gateway, symbol)
        if abs(current - quantity) > tolerance:
            return {
                "status": "stopped",
                "reason": "position_quantity_mismatch",
                "expected_quantity": decimal_text(quantity),
                "actual_quantity": decimal_text(current),
            }
        if current * Decimal(str(snapshot.mid)) >= max_position_quote:
            return {"status": "stopped", "reason": "position_notional_reaches_max_position"}
        result = execute_adaptive_maker_target(
            venue,
            AdaptiveMakerPolicy(REAL_POLICY),
            TargetRequest(
                side="sell",
                target_position=0,
                deadline_ms=timeout_seconds * 1000,
                poll_interval_ms=250,
                max_requotes=30,
                tolerance_quantity=float(tolerance),
                client_prefix=_prefix("flat", 0, "s"),
            ),
        )
        active = self.gateway.open_orders(None, mode="demo")
        final_position = venue.position_quantity()
        completed = (
            result.status == "completed" and result.maker_only and final_position <= float(tolerance) and not active
        )
        return {
            "status": "completed" if completed else result.status,
            "reason": "position_flattened" if completed else result.reason,
            "maker_only": result.maker_only,
            "final_position": final_position,
            "active_order_count": len(active),
            "execution": result.as_dict(),
            "policy": REAL_POLICY.as_dict(),
        }


class AdaptiveMakerVolumeService:
    def __init__(self, gateway: WeexGateway, *, venue_factory: VenueFactory = DemoAdaptiveMakerVenue) -> None:
        self.gateway = gateway
        self.venue_factory = venue_factory

    def run(self, plan: MakerVolumePlan) -> dict[str, Any]:
        started = time.monotonic()
        if self.gateway.open_orders(None, mode="demo"):
            return self._finish(plan, started, [], "stopped", "starting_open_orders_present")
        venue = self.venue_factory(self.gateway, plan.symbol)
        venue.snapshot()
        tolerance = _amount_tolerance(self.gateway, plan.symbol)
        if venue.position_quantity() > float(tolerance):
            return self._finish(plan, started, [], "stopped", "starting_position_not_flat")

        legs: list[dict[str, Any]] = []
        total_quote = Decimal("0")
        open_quantity = Decimal("0")
        for sequence in range(1, plan.fills + 1):
            action = "open" if sequence % 2 else "close"
            side = "buy" if action == "open" else "sell"
            if action == "open":
                remaining_volume = max(Decimal("0"), plan.target_quote - total_quote)
                remaining_legs = plan.fills - sequence + 1
                desired_quote = max(plan.target_quote / plan.fills, remaining_volume / remaining_legs) * VOLUME_BUFFER
                snapshot = venue.snapshot()
                open_quantity = self.gateway.amount_to_precision(
                    plan.symbol, desired_quote / Decimal(str(snapshot.mid))
                )
                if open_quantity <= 0 or open_quantity * Decimal(str(snapshot.ask)) >= plan.max_position_quote:
                    return self._finish(plan, started, legs, "stopped", "opening_quantity_outside_bounds")
                target_position = open_quantity
            else:
                target_position = Decimal("0")

            result = execute_adaptive_maker_target(
                venue,
                AdaptiveMakerPolicy(REAL_POLICY),
                TargetRequest(
                    side=side,  # type: ignore[arg-type]
                    target_position=float(target_position),
                    deadline_ms=plan.timeout_seconds * 1000,
                    poll_interval_ms=max(250, round(plan.poll_interval_seconds * 1000)),
                    max_requotes=30,
                    tolerance_quantity=float(tolerance),
                    client_prefix=_prefix("vol", sequence, side[0]),
                ),
            )
            total_quote += Decimal(str(result.quote_volume))
            legs.append({"sequence": sequence, "action": action, **result.as_dict()})
            if result.status != "completed" or not result.maker_only:
                final_position, active_order_count = self._final_state(venue)
                return self._finish(
                    plan,
                    started,
                    legs,
                    result.status,
                    result.reason,
                    final_position=final_position,
                    active_order_count=active_order_count,
                )
            if action == "close":
                open_quantity = Decimal("0")

        active = self.gateway.open_orders(None, mode="demo")
        final_position = venue.position_quantity()
        fill_count = sum(int(leg["fill_count"]) for leg in legs)
        completed = (
            total_quote >= plan.target_quote
            and len(legs) == plan.fills
            and fill_count >= plan.fills
            and all(bool(leg["maker_only"]) for leg in legs)
            and final_position <= float(tolerance)
            and not active
        )
        return self._finish(
            plan,
            started,
            legs,
            "completed" if completed else "stopped",
            "target_reached" if completed else "acceptance_invariant_failed",
            total_quote=total_quote,
            final_position=final_position,
            active_order_count=len(active),
        )

    def _final_state(self, venue: MakerVenue) -> tuple[float | None, int | None]:
        try:
            final_position = venue.position_quantity()
        except Exception:  # noqa: BLE001 - preserve the execution result when reconciliation is unavailable
            final_position = None
        try:
            active_order_count = len(self.gateway.open_orders(None, mode="demo"))
        except Exception:  # noqa: BLE001 - an unknown count must remain explicit in the report
            active_order_count = None
        return final_position, active_order_count

    @staticmethod
    def _finish(
        plan: MakerVolumePlan,
        started: float,
        legs: list[dict[str, Any]],
        status: str,
        reason: str,
        *,
        total_quote: Decimal | None = None,
        final_position: float | None = None,
        active_order_count: int | None = None,
    ) -> dict[str, Any]:
        quote = (
            total_quote
            if total_quote is not None
            else sum((Decimal(str(leg.get("quote_volume") or 0)) for leg in legs), Decimal("0"))
        )
        return {
            "status": status,
            "reason": reason,
            "plan": plan.as_dict(),
            "total_quote_volume": decimal_text(quote),
            "fill_count": sum(int(leg.get("fill_count") or 0) for leg in legs),
            "submission_count": sum(int(leg.get("submissions") or 0) for leg in legs),
            "policy_cancel_count": sum(int(leg.get("cancels") or 0) for leg in legs),
            "venue_cancel_count": sum(int(leg.get("venue_cancels") or 0) for leg in legs),
            "preflight_skip_count": sum(int(leg.get("preflight_skips") or 0) for leg in legs),
            "observation_error_count": sum(int(leg.get("observation_errors") or 0) for leg in legs),
            "cancel_verification_attempt_count": sum(int(leg.get("cancel_verification_attempts") or 0) for leg in legs),
            "cancel_verification_error_count": sum(int(leg.get("cancel_verification_errors") or 0) for leg in legs),
            "post_only_rejection_count": sum(int(leg.get("post_only_rejections") or 0) for leg in legs),
            "leg_count": len(legs),
            "cycles_completed": len(legs) // 2,
            "maker_only": all(bool(leg.get("maker_only")) for leg in legs),
            "final_position": final_position,
            "active_order_count": active_order_count,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "policy": REAL_POLICY.as_dict(),
            "legs": legs,
        }


class DemoMakerSoakService:
    def __init__(
        self,
        gateway: WeexGateway,
        *,
        volume_service_factory: VolumeServiceFactory = AdaptiveMakerVolumeService,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.gateway = gateway
        self.volume_service_factory = volume_service_factory
        self.sleep = sleep

    def run(self, plan: MakerSoakPlan) -> dict[str, Any]:
        started = time.monotonic()
        rounds: list[dict[str, Any]] = []
        for sequence in range(1, plan.rounds + 1):
            result = self.volume_service_factory(self.gateway).run(plan.volume_plan)
            rounds.append({"round": sequence, **result})
            if result.get("status") != "completed":
                return self._finish(plan, started, rounds, "failed", f"round_{sequence}_{result.get('reason')}")
            if sequence < plan.rounds:
                self.sleep(MIN_SUBMIT_INTERVAL_SECONDS)
        return self._finish(plan, started, rounds, "completed", "all_rounds_completed")

    @staticmethod
    def _finish(
        plan: MakerSoakPlan,
        started: float,
        rounds: list[dict[str, Any]],
        status: str,
        reason: str,
    ) -> dict[str, Any]:
        return {
            "status": status,
            "reason": reason,
            "plan": plan.as_dict(),
            "rounds_requested": plan.rounds,
            "rounds_completed": sum(1 for row in rounds if row.get("status") == "completed"),
            "total_quote_volume": decimal_text(
                sum((Decimal(str(row.get("total_quote_volume") or 0)) for row in rounds), Decimal("0"))
            ),
            "total_submissions": sum(int(row.get("submission_count") or 0) for row in rounds),
            "total_post_only_rejections": sum(int(row.get("post_only_rejection_count") or 0) for row in rounds),
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "rounds": rounds,
        }


def maker_flatten_confirmation(
    *, symbol: str, quantity: Decimal, max_position_quote: Decimal, timeout_seconds: int
) -> str:
    return " ".join(
        [
            "EXECUTE",
            "WEEX",
            "DEMO",
            "MAKER",
            "FLATTEN",
            symbol.upper(),
            f"QUANTITY_{decimal_text(quantity)}",
            f"MAX_POSITION_{decimal_text(max_position_quote)}",
            f"TIMEOUT_{timeout_seconds}",
        ]
    )


def maker_soak_confirmation(plan: MakerSoakPlan) -> str:
    volume = plan.volume_plan
    return " ".join(
        [
            "EXECUTE",
            "WEEX",
            "DEMO",
            "MAKER",
            "SOAK",
            volume.symbol.upper(),
            f"TARGET_{decimal_text(volume.target_quote)}",
            f"FILLS_{volume.fills}",
            f"ROUNDS_{plan.rounds}",
            f"MAX_POSITION_{decimal_text(volume.max_position_quote)}",
            f"TIMEOUT_{volume.timeout_seconds}",
        ]
    )


def _amount_tolerance(gateway: WeexGateway, symbol: str) -> Decimal:
    return gateway.amount_step(symbol) / 2


def _prefix(kind: str, sequence: int, side: str) -> str:
    return f"a{kind[0]}{uuid.uuid4().hex[:8]}{sequence:02d}{side}"
