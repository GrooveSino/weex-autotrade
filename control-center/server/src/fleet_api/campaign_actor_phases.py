"""Immediate opening, closing, finalization, and safe-stop Campaign phases."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from typing import Any

from weex_cli.beta_volume import (
    _is_uncertain_stop,
    _LegSpec,
    _signed_open_quantity,
    _size_cycle,
    _terminal_reason,
)
from weex_cli.models import decimal_text

from .campaign_actor_cycles import (
    close_lanes,
    cycle_record,
    observe_positions,
    positions_are_flat,
    safe_stop,
    sampled_delay,
    targets_reached,
)
from .campaign_actor_models import (
    BOUNDARY_COUNTS,
    Campaign,
    CampaignActorContext,
    CloseCycle,
    EnvironmentFactory,
    OpenCycle,
)
from .campaign_actor_planning import new_actor_context, prepare_cycle_leverage


class CampaignActorPhases:
    """Run bounded I/O segments while actor timers own non-I/O waiting."""

    def __init__(
        self,
        environment_factory: EnvironmentFactory,
        *,
        is_stopping: Callable[[], bool],
        ownership_sink: Callable[[OpenCycle, str], None] | None = None,
    ) -> None:
        self._environment_factory = environment_factory
        self._is_stopping = is_stopping
        self._ownership_sink = ownership_sink or (lambda _opened, _state: None)

    def prepare(self, campaign: Campaign) -> CampaignActorContext:
        environment = self._environment_factory("prepare")
        try:
            service = environment.campaign_service
            service.current_campaign = campaign
            service._validate_authorization(campaign)
            service._emit("campaign_boundary_started", phase="initial")
            boundary = service._read_boundary()
            service._emit("campaign_boundary_completed", phase="initial")
            if any(boundary.get(key) for key in BOUNDARY_COUNTS):
                raise RuntimeError("campaign requires a flat account boundary")
            service.campaign_store.claim_for_execution(campaign)
            return new_actor_context(service, campaign)
        finally:
            environment.close()

    def plan_open(self, campaign: Campaign, context: CampaignActorContext) -> OpenCycle:
        environment = self._environment_factory("open")
        try:
            service = environment.volume_service
            plan = context.child
            lanes = service._create_lanes(plan)
            service.current_plan_id = plan.plan_id
            preflight = service._preflight_with_read_retry(plan)
            desired_quote = min(plan.round_turnover_quote, plan.target_turnover_quote - context.child_total_quote)
            if desired_quote <= 0:
                raise RuntimeError("campaign child target is already complete")
            service._emit("cycle_preparing", round=context.round_number, desired_quote=decimal_text(desired_quote))
            btc_plan, eth_plan, sizing = service._read_with_retry(
                lambda: _size_cycle(plan, lanes, desired_quote),
                operation="cycle_sizing",
                retry_event="cycle_sizing_retry",
                round=context.round_number,
            )
            selected, leverage_state = prepare_cycle_leverage(service, plan, sizing, context.round_number)
            started_at_ms = service.now_ms()
            opened = OpenCycle(
                context=context,
                preflight=preflight,
                btc_plan=btc_plan,
                eth_plan=eth_plan,
                sizing=sizing,
                selected_leverage=selected,
                leverage_state=leverage_state,
                open_summaries=[],
                lane_stops={},
                started_at_ms=started_at_ms,
                hold_seconds=0,
            )
            self._ownership_sink(opened, "planned")
            return opened
        finally:
            environment.close()

    def execute_open(self, campaign: Campaign, opened: OpenCycle) -> None:
        environment = self._environment_factory("open")
        try:
            service = environment.volume_service
            plan = opened.context.child
            lanes = service._create_lanes(plan)
            service.current_plan_id = plan.plan_id
            results = self._open_pair(
                service,
                plan,
                opened.context.round_number,
                Decimal(str(opened.sizing["opening_notional_quote"])),
                opened.btc_plan,
                opened.eth_plan,
                lanes,
            )
            opened.open_summaries.extend(results[symbol][0] for symbol in ("BTC", "ETH"))
            opened.lane_stops.update({symbol: row[1] for symbol, row in results.items() if row[1] is not None})
            self._ownership_sink(opened, "opened")
            opened.hold_seconds = self._verify_open_target(
                service,
                lanes,
                opened.context.round_number,
                opened.btc_plan,
                opened.eth_plan,
                opened.lane_stops,
                campaign,
            )
            opened.hold_started_at_ms = service.now_ms() if opened.hold_seconds > 0 else None
        finally:
            environment.close()

    def open(self, campaign: Campaign, context: CampaignActorContext) -> OpenCycle:
        """Compatibility entry point for bounded callers outside the Actor program."""
        opened = self.plan_open(campaign, context)
        self.execute_open(campaign, opened)
        return opened

    def close(self, campaign: Campaign, opened: OpenCycle) -> CloseCycle:
        environment = self._environment_factory("close")
        try:
            service = environment.volume_service
            lanes = service._create_lanes(opened.context.child)
            service.current_plan_id = opened.context.child.plan_id
            if self._is_stopping():
                outcome = CloseCycle(Decimal(0), safe_stop(service, lanes, opened), "stop_requested", None, 0)
            else:
                outcome = self._close_cycle(service, lanes, campaign, opened)
            self._ownership_sink(opened, "closed" if outcome.uncertain_reason is None else "uncertain")
            return outcome
        finally:
            environment.close()

    def safe_stop(self, opened: OpenCycle) -> dict[str, Any]:
        environment = self._environment_factory("safe_stop")
        try:
            service = environment.volume_service
            result = safe_stop(service, service._create_lanes(opened.context.child), opened)
            self._ownership_sink(opened, "closed" if result.get("status") != "uncertain" else "uncertain")
            return result
        finally:
            environment.close()

    def finish(
        self,
        campaign: Campaign,
        context: CampaignActorContext,
        *,
        status: str,
        reason: str,
        child_result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        environment = self._environment_factory("finish")
        try:
            results = [child_result] if child_result is not None else []
            return environment.campaign_service._finish(
                campaign,
                status,
                reason,
                context.child_total_quote,
                results,
                context.execution_started_at_ms,
            )
        finally:
            environment.close()

    @staticmethod
    def _open_pair(
        service: Any,
        plan: Any,
        round_number: int,
        desired_quote: Decimal,
        btc_plan: Any,
        eth_plan: Any,
        lanes: Mapping[str, Any],
    ) -> Mapping[str, tuple[dict[str, Any], tuple[str, str] | None]]:
        service._emit(
            "cycle_started",
            round=round_number,
            desired_quote=decimal_text(desired_quote),
            btc_quantity=decimal_text(btc_plan.quantity),
            eth_quantity=decimal_text(eth_plan.quantity),
        )
        specs = {
            "BTC": _LegSpec(
                btc_plan,
                "open",
                btc_plan.opening_side,
                _signed_open_quantity(btc_plan),
                f"{plan.plan_id}-r{round_number:03d}-bo",
            ),
            "ETH": _LegSpec(
                eth_plan,
                "open",
                eth_plan.opening_side,
                _signed_open_quantity(eth_plan),
                f"{plan.plan_id}-r{round_number:03d}-eo",
            ),
        }
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="fleet-open") as pool:
            return service._run_pair(pool, plan, round_number, 1, specs, lanes)

    def _verify_open_target(
        self,
        service: Any,
        lanes: Mapping[str, Any],
        round_number: int,
        btc_plan: Any,
        eth_plan: Any,
        stops: Mapping[str, tuple[str, str]],
        campaign: Campaign,
    ) -> float:
        if stops:
            return 0
        positions = observe_positions(service, lanes, round_number, action="hold_check")
        if not targets_reached(positions, btc_plan, eth_plan):
            service._emit("open_barrier_not_ready", round=round_number)
            return 0
        delay = sampled_delay(campaign.hold_min_seconds, campaign.hold_max_seconds)
        service._emit("open_barrier_verified", round=round_number)
        service._emit("hold_started", round=round_number, seconds=delay)
        return delay

    def _close_cycle(
        self,
        service: Any,
        lanes: Mapping[str, Any],
        campaign: Campaign,
        opened: OpenCycle,
    ) -> CloseCycle:
        context = opened.context
        stops = dict(opened.lane_stops)
        service._emit("close_barrier_started", round=context.round_number)
        close_summaries = close_lanes(service, lanes, opened, stops)
        service._emit("pair_wait_completed", round=context.round_number, action="close")
        legs = opened.open_summaries + close_summaries
        service._refresh_pending_accounting(context.round_number, legs, lanes, stops)
        positions = observe_positions(service, lanes, context.round_number)
        flat = positions_are_flat(positions, opened.btc_plan, opened.eth_plan)
        quote = sum((Decimal(str(row.get("quote_volume") or 0)) for row in legs), Decimal(0))
        context.child_total_quote += quote
        context.summaries.extend(legs)
        reason = _terminal_reason(stops)
        uncertain = any(_is_uncertain_stop(stop) for stop in stops.values())
        context.empty_rounds = context.empty_rounds + 1 if quote == 0 else 0
        if reason is None and context.empty_rounds > context.child.max_empty_rounds:
            reason = "maximum_empty_rounds"
        should_continue = (
            not uncertain
            and reason is None
            and flat
            and context.child_total_quote < context.child.target_turnover_quote
        )
        gap = sampled_delay(campaign.round_gap_min_seconds, campaign.round_gap_max_seconds) if should_continue else 0
        record = cycle_record(
            opened,
            legs,
            quote,
            positions,
            flat=flat,
            reason=reason,
            uncertain=uncertain,
            round_gap_seconds=gap,
            elapsed_ms=max(0, service.now_ms() - opened.started_at_ms),
        )
        context.cycles.append(record)
        service._emit(
            "cycle_completed" if record["status"] in {"completed", "recovered"} else "cycle_stopped",
            round=context.round_number,
            status=record["status"],
            reason=record["reason"],
            quote_volume=decimal_text(quote),
            total_quote=decimal_text(context.child_total_quote),
            elapsed_ms=record["elapsed_ms"],
        )
        if uncertain:
            return CloseCycle(quote, None, None, reason or "lane_execution_uncertain", 0)
        if reason is not None or not flat:
            return CloseCycle(quote, None, reason or "paired_cycle_not_flat", None, 0)
        if context.child_total_quote >= context.child.target_turnover_quote:
            result = service._final_acceptance(
                context.child,
                context.summaries,
                context.cycles,
                context.child_total_quote,
                lanes,
                opened.preflight,
                context.execution_started_at_ms,
            )
            return CloseCycle(quote, result, None, None, 0)
        context.round_number += 1
        service._emit("round_gap_started", round=context.round_number - 1, seconds=gap)
        return CloseCycle(quote, None, None, None, gap)
