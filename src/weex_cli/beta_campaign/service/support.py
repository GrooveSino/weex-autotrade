"""Campaign child-plan construction and read-side recovery helpers."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from decimal import Decimal
from typing import Any

from weex_cli.beta_volume import BetaVolumePlan, LiveBetaVolumeService, inspect_live_account
from weex_cli.core.errors import SafetyError, ValidationError
from weex_cli.core.reliability import NETWORK_ERRORS, retry_read

from ..helpers import _selected_round_turnover
from ..model import CAMPAIGN_READ_RETRY_POLICY, BetaVolumeCampaign


class _CampaignSupportMixin:
    def _validate_authorization(self, campaign: BetaVolumeCampaign) -> None:
        if campaign.schema_version not in {1, 2, 3, 4, 5}:
            raise SafetyError("unsupported campaign schema")
        if campaign.profile_fingerprint != self.profile_fingerprint:
            raise SafetyError("campaign was authorized for a different live profile")
        if self.now_ms() >= campaign.expires_at_ms:
            raise SafetyError("campaign authorization expired; create a new dry run")
        self._read_with_retry(self.provider.get, operation="beta_allocation")

    def _create_child(self, campaign: BetaVolumeCampaign, target: Decimal, run_number: int) -> BetaVolumePlan:
        created_at_ms = self.now_ms() + run_number
        round_quote = _selected_round_turnover(campaign, target, run_number)
        try:
            return BetaVolumePlan.create(
                self.gateway,
                campaign.allocation,
                target_turnover_quote=target,
                round_turnover_quote=round_quote,
                max_position_quote=campaign.max_position_quote,
                timeout_seconds=campaign.timeout_seconds,
                recovery_attempts=campaign.recovery_attempts,
                max_empty_rounds=campaign.max_empty_rounds,
                cooldown_seconds=campaign.cooldown_seconds,
                leverage=campaign.leverage,
                max_auto_leverage=campaign.max_auto_leverage,
                margin_buffer=campaign.margin_buffer,
                margin_mode=campaign.margin_mode,
                direction=campaign.direction,
                dust_close_max_quote=campaign.dust_close_max_quote,
                now_ms=created_at_ms,
            )
        except ValidationError as exc:
            if "below the current" not in str(exc):
                raise
            fallback_target = min(round_quote, campaign.authorized_max_turnover_quote - target)
            if fallback_target <= 0:
                raise
            return BetaVolumePlan.create(
                self.gateway,
                campaign.allocation,
                target_turnover_quote=fallback_target,
                round_turnover_quote=fallback_target,
                max_position_quote=campaign.max_position_quote,
                timeout_seconds=campaign.timeout_seconds,
                recovery_attempts=campaign.recovery_attempts,
                max_empty_rounds=campaign.max_empty_rounds,
                cooldown_seconds=campaign.cooldown_seconds,
                leverage=campaign.leverage,
                max_auto_leverage=campaign.max_auto_leverage,
                margin_buffer=campaign.margin_buffer,
                margin_mode=campaign.margin_mode,
                direction=campaign.direction,
                dust_close_max_quote=campaign.dust_close_max_quote,
                now_ms=created_at_ms,
            )

    def _execute_child_with_read_retry(self, child: BetaVolumePlan) -> dict[str, Any]:
        for attempt in range(1, CAMPAIGN_READ_RETRY_POLICY.attempts + 1):
            try:
                return self.child_executor(child)
            except NETWORK_ERRORS:
                state = self.child_store.load_record(child.plan_id).state
                if state != "planned" or attempt >= CAMPAIGN_READ_RETRY_POLICY.attempts:
                    raise
                delay = CAMPAIGN_READ_RETRY_POLICY.delay_after(attempt)
                self._emit(
                    "campaign_read_retry",
                    child_plan_id=child.plan_id,
                    operation="child_preflight",
                    attempt=attempt + 1,
                    max_attempts=CAMPAIGN_READ_RETRY_POLICY.attempts,
                    seconds=delay,
                )
                self.sleep(delay)
        raise AssertionError("unreachable")

    def _read_with_retry(
        self,
        reader: Callable[[], Any],
        *,
        operation: str,
        **fields: Any,
    ) -> Any:
        def on_retry(event: Mapping[str, object]) -> None:
            self._emit(
                "campaign_read_retry",
                operation=operation,
                attempt=event.get("next_attempt"),
                max_attempts=event.get("max_attempts"),
                seconds=event.get("delay_seconds"),
                error=event.get("error"),
                **fields,
            )

        return retry_read(
            reader,
            operation=operation,
            policy=CAMPAIGN_READ_RETRY_POLICY,
            sleep=self.sleep,
            retry_sink=on_retry,
        )

    def _execute_child(self, child: BetaVolumePlan) -> dict[str, Any]:
        campaign = self.current_campaign
        if campaign is None:
            raise SafetyError("campaign timing policy is unavailable")
        service = LiveBetaVolumeService(
            self.gateway,
            self.provider,
            self.child_store,
            event_sink=self.event_sink,
            lane_gateways=self.lane_gateways,
            market_data=self.market_data,
            order_updates=self.order_updates,
            stop_requested=self.stop_requested,
            phase_waiter=self.phase_waiter,
            now_ms=self.now_ms,
            sleep=self.sleep,
            hold_delay_seconds=lambda round_number: self._sample_delay(
                campaign.hold_min_seconds,
                campaign.hold_max_seconds,
            ),
            round_gap_delay_seconds=lambda round_number: self._sample_delay(
                campaign.round_gap_min_seconds,
                campaign.round_gap_max_seconds,
            ),
        )
        return service.execute(child)

    def _sample_delay(self, minimum: float, maximum: float) -> float:
        if minimum == maximum:
            return minimum
        return self.uniform(minimum, maximum)

    def _read_boundary(self) -> dict[str, Any]:
        return self._read_with_retry(
            lambda: inspect_live_account(self.gateway, Decimal(0)),
            operation="account_boundary",
        )
