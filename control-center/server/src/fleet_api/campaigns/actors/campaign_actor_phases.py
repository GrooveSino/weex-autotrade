"""Immediate opening, closing, finalization, and safe-stop Campaign phases."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import suppress
from decimal import Decimal
from typing import Any

from weex_cli.beta_allocation import BetaUnavailable
from weex_cli.beta_volume import _is_uncertain_stop, _terminal_reason
from weex_cli.models import decimal_text

from fleet_api.campaigns.actors.campaign_actor_cycles import (
    close_lanes,
    cycle_record,
    observe_positions,
    positions_are_flat,
    safe_stop,
    sampled_delay,
    targets_reached,
)
from fleet_api.campaigns.actors.campaign_actor_models import (
    Campaign,
    CampaignActorContext,
    CloseCycle,
    CycleCondition,
    CycleConditionError,
    EnvironmentFactory,
    OpenCycle,
)
from fleet_api.campaigns.actors.campaign_actor_planning import (
    build_cycle_plan,
    check_cycle_conditions,
    new_actor_context,
    prepare_cycle_leverage,
    retry_cycle_condition,
)
from fleet_api.campaigns.actors.phase_helpers.open_pair import open_pair
from fleet_api.campaigns.actors.targets.target_policy import campaign_completion_floor, emit_tolerance_acceptance


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
            # Static authorization checks run before the provider read. The
            # normal condition loop owns temporary Beta outages.
            with suppress(BetaUnavailable):
                service._validate_authorization(campaign)
            service.campaign_store.claim_for_execution(campaign)
            return new_actor_context(service, campaign)
        finally:
            environment.close()

    def prepare_for_resume(self, campaign: Campaign) -> None:
        """Reclaim a confirmed condition wait without replaying preview drift checks."""
        environment = self._environment_factory("prepare")
        try:
            service = environment.campaign_service
            service.current_campaign = campaign
            if campaign.schema_version not in {1, 2, 3, 4, 5}:
                raise RuntimeError("unsupported campaign schema")
            if campaign.profile_fingerprint != service.profile_fingerprint:
                raise RuntimeError("campaign was authorized for a different live profile")
            service.campaign_store.claim_for_execution(campaign)
        finally:
            environment.close()

    def plan_open(self, campaign: Campaign, context: CampaignActorContext) -> OpenCycle:
        environment = self._environment_factory("open")
        try:
            service = environment.volume_service
            service._emit("cycle_preparing", round=context.round_number, attempt=context.attempt_number + 1)
            plan, preflight, btc_plan, eth_plan, sizing, lanes = build_cycle_plan(service, context)
            service.current_plan_id = plan.plan_id
            try:
                selected, leverage_state = prepare_cycle_leverage(service, plan, sizing, context.round_number)
            except Exception as exc:
                raise CycleConditionError(
                    CycleCondition(
                        code="leverage_configuration_unavailable",
                        detail="杠杆准备暂时无法完成，系统不会下单并会自动重新检查",
                        action="等待账户配置恢复",
                    )
                ) from exc
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
                execution_plan=plan,
            )
            try:
                self._ownership_sink(opened, "planned")
            except Exception as exc:
                raise CycleConditionError(
                    CycleCondition(
                        code="persistence_unavailable",
                        detail="执行快照暂时无法持久化，系统不会下单并会自动重新检查",
                        action="等待本地执行记录恢复",
                    )
                ) from exc
            return opened
        finally:
            environment.close()

    def check_open_conditions(self, context: CampaignActorContext) -> None:
        environment = self._environment_factory("condition")
        try:
            check_cycle_conditions(environment.volume_service, context)
        finally:
            environment.close()

    def execute_open(self, campaign: Campaign, opened: OpenCycle) -> None:
        environment = self._environment_factory("open")
        try:
            service = environment.volume_service
            plan = opened.plan
            lanes = service._create_lanes(plan)
            service.current_plan_id = plan.plan_id
            results = open_pair(
                service,
                plan,
                opened.context.round_number,
                Decimal(str(opened.sizing["planned_turnover_quote"])),
                max(opened.context.child.target_turnover_quote - opened.context.child_total_quote, Decimal(0)),
                opened.btc_plan,
                opened.eth_plan,
                lanes,
            )
            opened.open_summaries.extend(results[symbol][0] for symbol in ("BTC", "ETH"))
            opened.lane_stops.update({symbol: row[1] for symbol, row in results.items() if row[1] is not None})
            self._ownership_sink(opened, "opened")
            opened.hold_seconds, opened.hold_started_at_ms = self._verify_open_target(
                service,
                lanes,
                opened.context.round_number,
                opened.btc_plan,
                opened.eth_plan,
                opened.lane_stops,
                campaign,
            )
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
            lanes = service._create_lanes(opened.plan)
            service.current_plan_id = opened.plan.plan_id
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
            result = safe_stop(service, service._create_lanes(opened.plan), opened)
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

    def _verify_open_target(
        self,
        service: Any,
        lanes: Mapping[str, Any],
        round_number: int,
        btc_plan: Any,
        eth_plan: Any,
        stops: Mapping[str, tuple[str, str]],
        campaign: Campaign,
    ) -> tuple[float, int | None]:
        if stops:
            return 0, None
        positions = observe_positions(service, lanes, round_number, action="hold_check")
        if not targets_reached(positions, btc_plan, eth_plan):
            service._emit("open_barrier_not_ready", round=round_number)
            return 0, None
        delay = sampled_delay(campaign.hold_min_seconds, campaign.hold_max_seconds)
        started_at_ms = service.now_ms()
        service._emit("open_barrier_verified", round=round_number)
        service._emit(
            "hold_started",
            round=round_number,
            seconds=delay,
            started_at_ms=started_at_ms,
            deadline_at_ms=started_at_ms + int(delay * 1_000),
        )
        return delay, started_at_ms

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
        retry_condition = retry_cycle_condition(reason, quote, flat, uncertain)
        record_reason = None if retry_condition is not None else reason
        completion_floor = campaign_completion_floor(context)
        should_continue = (
            not uncertain
            and record_reason is None
            and flat
            and retry_condition is None
            and completion_floor is None
        )
        gap = sampled_delay(campaign.round_gap_min_seconds, campaign.round_gap_max_seconds) if should_continue else 0
        record = cycle_record(
            opened,
            legs,
            quote,
            positions,
            flat=flat,
            reason=record_reason,
            uncertain=uncertain,
            round_gap_seconds=gap,
            elapsed_ms=max(0, service.now_ms() - opened.started_at_ms),
        )
        context.cycles.append(record)
        service._emit(
            "cycle_completed" if record["status"] in {"completed", "recovered", "empty"} else "cycle_stopped",
            round=context.round_number,
            status=record["status"],
            reason=record["reason"],
            quote_volume=decimal_text(quote),
            total_quote=decimal_text(context.child_total_quote),
            target_quote=decimal_text(context.child.target_turnover_quote),
            remaining_quote=decimal_text(
                max(context.child.target_turnover_quote - context.child_total_quote, Decimal(0))
            ),
            elapsed_ms=record["elapsed_ms"],
        )
        if uncertain:
            return CloseCycle(quote, None, None, reason or "lane_execution_uncertain", 0)
        if completion_floor is not None:
            emit_tolerance_acceptance(service, context, completion_floor)
            result = service._final_acceptance(
                context.child,
                context.summaries,
                context.cycles,
                context.child_total_quote,
                lanes,
                opened.preflight,
                context.execution_started_at_ms,
                minimum_accepted_quote=completion_floor,
            )
            return CloseCycle(quote, result, None, None, 0)
        if retry_condition is not None:
            if quote > 0:
                context.round_number += 1
            return CloseCycle(quote, None, None, None, 0, condition=retry_condition)
        if reason is not None or not flat:
            return CloseCycle(quote, None, reason or "paired_cycle_not_flat", None, 0)
        context.round_number += 1
        gap_started_at_ms = service.now_ms()
        service._emit(
            "round_gap_started",
            round=context.round_number - 1,
            seconds=gap,
            started_at_ms=gap_started_at_ms,
            deadline_at_ms=gap_started_at_ms + int(gap * 1_000),
        )
        return CloseCycle(quote, None, None, None, gap, gap_started_at_ms)
