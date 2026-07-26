"""Campaign child-run loop."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from weex_cli.core.errors import SafetyError
from weex_cli.core.models import decimal_text
from weex_cli.core.reliability import NETWORK_ERRORS

from ..helpers import _authoritative_child_quote, _boundary_is_flat
from ..model import RETRYABLE_CHILD_REASONS, BetaVolumeCampaign


class _CampaignExecutionMixin:
    def execute(self, campaign: BetaVolumeCampaign) -> dict[str, Any]:
        started_ms = self.now_ms()
        self.current_campaign = campaign
        self._validate_authorization(campaign)
        self._emit("campaign_boundary_started", phase="initial")
        initial_boundary = self._read_boundary()
        self._emit("campaign_boundary_completed", phase="initial")
        if not _boundary_is_flat(initial_boundary):
            raise SafetyError("campaign requires flat BTC/ETH positions and no regular or trigger orders")
        self.campaign_store.claim_for_execution(campaign)

        child_results: list[dict[str, Any]] = []
        total_quote = Decimal(0)
        for run_number in range(1, campaign.max_runs + 1):
            if self.stop_requested():
                return self._finish(campaign, "stopped", "stop_requested", total_quote, child_results, started_ms)
            if total_quote >= campaign.target_turnover_quote:
                break
            remaining = campaign.target_turnover_quote - total_quote
            self._emit(
                "campaign_child_planning_started",
                campaign_id=campaign.campaign_id,
                run=run_number,
                remaining_quote=decimal_text(remaining),
            )
            try:
                child = self._read_with_retry(
                    lambda remaining=remaining, run_number=run_number: self._create_child(
                        campaign, remaining, run_number
                    ),
                    operation="child_planning",
                    run=run_number,
                )
            except NETWORK_ERRORS as exc:
                return self._finish(
                    campaign,
                    "stopped",
                    f"child_planning_network:{type(exc).__name__.lower()}",
                    total_quote,
                    child_results,
                    started_ms,
                )
            self._emit(
                "campaign_child_planning_completed",
                campaign_id=campaign.campaign_id,
                run=run_number,
                child_plan_id=child.plan_id,
            )
            self.child_store.create(child)
            self._emit(
                "campaign_run_started",
                campaign_id=campaign.campaign_id,
                run=run_number,
                child_plan_id=child.plan_id,
                remaining_quote=decimal_text(remaining),
            )
            try:
                child_result = self._execute_child_with_read_retry(child)
            except NETWORK_ERRORS as exc:
                child_state = self.child_store.load_record(child.plan_id).state
                status = "stopped" if child_state == "planned" else "uncertain"
                reason = f"child_{child_state}_network:{type(exc).__name__.lower()}"
                return self._finish(campaign, status, reason, total_quote, child_results, started_ms)
            except Exception as exc:  # noqa: BLE001 - campaign must checkpoint before returning control
                child_state = self.child_store.load_record(child.plan_id).state
                status = "stopped" if child_state == "planned" else "uncertain"
                reason = f"child_{child_state}_exception:{type(exc).__name__.lower()}"
                return self._finish(campaign, status, reason, total_quote, child_results, started_ms)

            child_results.append(child_result)
            try:
                child_quote = _authoritative_child_quote(child_result)
            except SafetyError:
                return self._finish(
                    campaign,
                    "stopped",
                    "child_accounting_not_verified_pure_maker",
                    total_quote,
                    child_results,
                    started_ms,
                )
            total_quote += child_quote
            if total_quote > campaign.authorized_max_turnover_quote:
                return self._finish(
                    campaign,
                    "uncertain",
                    "authorized_volume_ceiling_exceeded",
                    total_quote,
                    child_results,
                    started_ms,
                )

            self._emit("campaign_boundary_started", phase="checkpoint", run=run_number)
            try:
                boundary = self._read_boundary()
            except NETWORK_ERRORS:
                return self._finish(
                    campaign,
                    "uncertain",
                    "child_boundary_observation_unavailable",
                    total_quote,
                    child_results,
                    started_ms,
                    {"observation": "unavailable"},
                )
            self._emit("campaign_boundary_completed", phase="checkpoint", run=run_number)
            checkpoint = self._result(
                campaign,
                "executing",
                "child_checkpointed",
                total_quote,
                child_results,
                boundary,
                started_ms,
            )
            self.campaign_store.save(campaign, state="executing", result=checkpoint)
            self._emit(
                "campaign_run_completed",
                campaign_id=campaign.campaign_id,
                run=run_number,
                child_plan_id=child.plan_id,
                child_status=child_result.get("status"),
                child_quote=decimal_text(child_quote),
                total_quote=decimal_text(total_quote),
            )

            if not _boundary_is_flat(boundary):
                return self._finish(
                    campaign,
                    "uncertain",
                    "child_finished_without_confirmed_flat_boundary",
                    total_quote,
                    child_results,
                    started_ms,
                    boundary,
                )
            if child_result.get("status") == "uncertain":
                return self._finish(
                    campaign,
                    "uncertain",
                    str(child_result.get("reason") or "child_uncertain"),
                    total_quote,
                    child_results,
                    started_ms,
                    boundary,
                )
            if self.stop_requested():
                return self._finish(
                    campaign,
                    "stopped",
                    "stop_requested",
                    total_quote,
                    child_results,
                    started_ms,
                    boundary,
                )
            child_completed = child_result.get("status") == "completed"
            if not child_completed and child_result.get("reason") not in RETRYABLE_CHILD_REASONS:
                return self._finish(
                    campaign,
                    "stopped",
                    str(child_result.get("reason") or "child_stopped"),
                    total_quote,
                    child_results,
                    started_ms,
                    boundary,
                )
            if total_quote >= campaign.target_turnover_quote:
                if not child_completed:
                    return self._finish(
                        campaign,
                        "stopped",
                        "target_reached_by_noncompleted_child",
                        total_quote,
                        child_results,
                        started_ms,
                        boundary,
                    )
                return self._finish(
                    campaign,
                    "completed",
                    "campaign_target_completed",
                    total_quote,
                    child_results,
                    started_ms,
                    boundary,
                )
            if child_completed:
                return self._finish(
                    campaign,
                    "stopped",
                    "child_completed_below_campaign_target",
                    total_quote,
                    child_results,
                    started_ms,
                    boundary,
                )

        return self._finish(
            campaign,
            "stopped",
            "campaign_run_limit_exhausted",
            total_quote,
            child_results,
            started_ms,
        )
