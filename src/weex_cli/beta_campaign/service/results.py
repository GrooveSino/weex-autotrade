"""Campaign terminal result projection and event publication."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from weex_cli.core.errors import SafetyError
from weex_cli.core.models import decimal_text
from weex_cli.core.reliability import NETWORK_ERRORS

from ..helpers import _child_is_authoritative, _child_is_pure_maker, _child_quote
from ..model import BetaVolumeCampaign


class _CampaignResultMixin:
    def _finish(
        self,
        campaign: BetaVolumeCampaign,
        status: str,
        reason: str,
        total_quote: Decimal,
        child_results: list[dict[str, Any]],
        started_ms: int,
        boundary: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if boundary is None:
            try:
                boundary = self._read_boundary()
            except NETWORK_ERRORS:
                boundary = {"observation": "unavailable"}
                status = "uncertain"
                reason = "final_boundary_observation_unavailable"
        result = self._result(campaign, status, reason, total_quote, child_results, boundary, started_ms)
        self.campaign_store.save(campaign, state=status, result=result)
        self._emit(
            "campaign_finished",
            campaign_id=campaign.campaign_id,
            status=status,
            reason=reason,
            total_quote=decimal_text(total_quote),
        )
        return result

    def _result(
        self,
        campaign: BetaVolumeCampaign,
        status: str,
        reason: str,
        total_quote: Decimal,
        child_results: list[dict[str, Any]],
        boundary: Mapping[str, Any],
        started_ms: int,
    ) -> dict[str, Any]:
        positive_children: list[dict[str, Any]] = []
        accounting_parse_failed = False
        for row in child_results:
            try:
                if _child_quote(row) > 0:
                    positive_children.append(row)
            except SafetyError:
                accounting_parse_failed = True
        return {
            "schema_version": 1,
            "kind": "beta_volume_campaign_execution",
            "mode": "live",
            "status": status,
            "reason": reason,
            "campaign_id": campaign.campaign_id,
            "target_turnover_quote": decimal_text(campaign.target_turnover_quote),
            "executed_quote_volume": decimal_text(total_quote),
            "remaining_quote": decimal_text(max(Decimal(0), campaign.target_turnover_quote - total_quote)),
            "excess_quote": decimal_text(max(Decimal(0), total_quote - campaign.target_turnover_quote)),
            "authorized_max_turnover_quote": decimal_text(campaign.authorized_max_turnover_quote),
            "maker_only": (
                not accounting_parse_failed
                and bool(positive_children)
                and all(_child_is_pure_maker(row) for row in positive_children)
            ),
            "liquidity_policy_satisfied": (
                not accounting_parse_failed
                and bool(positive_children)
                and all(_child_is_authoritative(row) for row in positive_children)
            ),
            "runs_used": len(child_results),
            "max_runs": campaign.max_runs,
            "elapsed_ms": self.now_ms() - started_ms,
            "final_boundary": dict(boundary),
            "children": child_results,
            "retry_allowed": False,
        }

    def _emit(self, event: str, **payload: Any) -> None:
        if self.event_sink is not None:
            self.event_sink({"event": event, **payload})
