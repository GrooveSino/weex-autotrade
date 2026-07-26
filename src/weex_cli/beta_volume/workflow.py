from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from weex_cli.beta_campaign.allocation import HttpBetaAllocationProvider
from weex_cli.beta_volume import (
    BetaVolumePlan,
    BetaVolumePlanStore,
    EventSink,
    GatewayFactory,
    LiveBetaVolumeService,
    ReconcilerFactory,
    beta_volume_confirmation,
    inspect_live_account,
)
from weex_cli.exchange.rest.gateway import WeexGateway


@dataclass(frozen=True)
class BetaVolumePlanRequest:
    target_turnover_quote: str | Decimal
    round_turnover_quote: str | Decimal = "500"
    max_position_quote: str | Decimal = "1200"
    timeout_seconds: int = 240
    recovery_attempts: int = 3
    max_empty_rounds: int = 3
    cooldown_seconds: float = 1.0
    leverage: str | int = "auto"
    margin_mode: str = "isolated"


class BetaVolumeApplication:
    """Reusable application boundary for CLI and control-plane adapters."""

    def __init__(
        self,
        gateway: WeexGateway,
        store: BetaVolumePlanStore,
        *,
        gateway_factory: GatewayFactory | None = None,
    ) -> None:
        self.gateway = gateway
        self.store = store
        self.gateway_factory = gateway_factory

    def create_plan(
        self,
        request: BetaVolumePlanRequest,
        provider: HttpBetaAllocationProvider,
    ) -> dict[str, Any]:
        plan = BetaVolumePlan.create(
            self.gateway,
            provider.get(),
            target_turnover_quote=request.target_turnover_quote,
            round_turnover_quote=request.round_turnover_quote,
            max_position_quote=request.max_position_quote,
            timeout_seconds=request.timeout_seconds,
            recovery_attempts=request.recovery_attempts,
            max_empty_rounds=request.max_empty_rounds,
            cooldown_seconds=request.cooldown_seconds,
            leverage=request.leverage,
            margin_mode=request.margin_mode,
        )
        readiness = inspect_live_account(
            self.gateway,
            plan.required_available_quote,
            opening_notional=plan.estimated_turnover_quote / 2,
            leverage=plan.leverage,
            max_auto_leverage=plan.max_auto_leverage,
            margin_buffer=plan.margin_buffer,
        )
        self.store.create(plan)
        return {
            "schema_version": 3,
            "kind": "beta_volume_plan",
            "status": "dry_run",
            "plan": plan.as_dict(),
            "account_readiness": readiness,
            "confirm": beta_volume_confirmation(plan),
            "execute_command": (
                f"./weex live beta-volume --execute --plan {plan.plan_id} --confirm '{beta_volume_confirmation(plan)}'"
            ),
            "safety": {
                "post_only": True,
                "authoritative_fill_reconciliation": True,
                "post_flat_accounting_attempts": 8,
                "parallel_lanes": 2,
                "beta_latched_for_session": True,
                "confidence_enforced": False,
                "no_automatic_submit_retry": True,
                "no_price_chasing": True,
                "stop_on_stranded_exposure": True,
                "leverage": (
                    f"recomputed from the available wallet before every cycle (max {plan.max_auto_leverage}x)"
                    if plan.leverage == "auto"
                    else f"fixed BTC and ETH isolated {plan.leverage}x"
                ),
                "recovery": "Create and separately confirm a pure-Maker flatten plan after inspecting live state.",
            },
        }

    def load_plan(self, plan_id: str) -> BetaVolumePlan:
        plan, _ = self.store.load(plan_id)
        return plan

    def execute_plan(
        self,
        plan: BetaVolumePlan,
        provider: HttpBetaAllocationProvider,
        *,
        reconciler_factory: ReconcilerFactory | None = None,
        event_sink: EventSink | None = None,
    ) -> dict[str, Any]:
        return LiveBetaVolumeService(
            self.gateway,
            provider,
            self.store,
            gateway_factory=self.gateway_factory,
            reconciler_factory=reconciler_factory,
            event_sink=event_sink,
        ).execute(plan)

    def recover_plan(
        self,
        plan: BetaVolumePlan,
        symbol: str,
        quantity: Decimal,
        *,
        event_sink: EventSink | None = None,
    ) -> dict[str, Any]:
        self.store.claim_for_recovery(plan, symbol)
        return LiveBetaVolumeService(self.gateway, None, self.store, event_sink=event_sink).recover(
            plan, symbol, quantity
        )
