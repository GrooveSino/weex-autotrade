from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from weex_cli.beta_campaign import (
    BetaVolumeCampaign,
    BetaVolumeCampaignRecord,
    BetaVolumeCampaignStore,
    LiveBetaVolumeCampaignService,
    campaign_confirmation,
    campaign_id_from_confirmation,
    campaign_plan_payload,
    live_profile_fingerprint,
)
from weex_cli.beta_campaign.allocation import BetaUnavailable, HttpBetaAllocationProvider
from weex_cli.beta_volume import BetaVolumePlanStore, inspect_live_account
from weex_cli.core.errors import SafetyError
from weex_cli.core.safety import require_execution
from weex_cli.exchange.rest.gateway import WeexGateway
from weex_cli.live_profile import LiveProfile
from weex_cli.live_websocket import WeexCampaignWebSocketRuntime


class CampaignEventSink(Protocol):
    def __call__(self, event: Mapping[str, Any]) -> None: ...


class CampaignRuntime(Protocol):
    def start(self) -> None: ...

    def close(self) -> None: ...


GatewayFactory = Callable[[], WeexGateway]
ProviderFactory = Callable[[], HttpBetaAllocationProvider]
RuntimeFactory = Callable[[WeexGateway, LiveProfile], CampaignRuntime]


@dataclass(frozen=True, slots=True)
class CampaignPreviewRequest:
    target_quote: str = "6000"
    cycle_volume: str = "500"
    hold_min_seconds: float = 300.0
    hold_max_seconds: float = 420.0
    round_gap_min_seconds: float = 300.0
    round_gap_max_seconds: float = 420.0


@dataclass(frozen=True, slots=True)
class CampaignRuntimePaths:
    campaigns: Path
    plans: Path

    @classmethod
    def for_account(cls, root: Path, account_id: str) -> CampaignRuntimePaths:
        account_root = root / "accounts" / account_id
        return cls(campaigns=account_root / "campaigns", plans=account_root / "plans")


class BetaCampaignApplication:
    """Shared live Campaign boundary used by the CLI and Textual adapters."""

    def __init__(
        self,
        profile: LiveProfile,
        paths: CampaignRuntimePaths,
        *,
        gateway_factory: GatewayFactory | None = None,
        provider_factory: ProviderFactory | None = None,
        runtime_factory: RuntimeFactory | None = None,
        now_ms: Callable[[], int] = lambda: int(time.time() * 1000),
    ) -> None:
        self.profile = profile
        self.paths = paths
        self.gateway_factory = gateway_factory or (lambda: WeexGateway(profile.settings, proxy_url=profile.proxy_url))
        if provider_factory is None:
            raise ValueError("Beta Campaign requires an explicit allocation provider")
        self.provider_factory = provider_factory
        self.runtime_factory = runtime_factory or self._default_runtime
        self.now_ms = now_ms
        self.campaign_store = BetaVolumeCampaignStore(paths.campaigns)
        self.child_store = BetaVolumePlanStore(paths.plans)
        self.profile_fingerprint = live_profile_fingerprint(profile)

    def account_snapshot(self) -> dict[str, Any]:
        gateway = self.gateway_factory()
        try:
            readiness = inspect_live_account(gateway, Decimal(0))
            snapshot = {
                "mode": "live",
                "api_status": "ok",
                "available_quote": readiness.get("available_quote", "0"),
                "active_position_count": readiness["active_position_count"],
                "position_sizes": readiness.get("position_sizes", {"BTC": "0", "ETH": "0"}),
                "regular_order_count": readiness["regular_order_count"],
                "trigger_order_count": readiness["trigger_order_count"],
            }
            try:
                allocation = self.provider_factory().get()
            except BetaUnavailable as exc:
                snapshot.update(
                    {
                        "allocation_status": "unavailable",
                        "allocation_error": str(exc),
                    }
                )
            else:
                snapshot.update(
                    {
                        "allocation_status": "ok",
                        "allocation": allocation.as_dict(),
                    }
                )
            return snapshot
        finally:
            _close_gateway(gateway)

    def account_boundary(self) -> dict[str, Any]:
        gateway = self.gateway_factory()
        try:
            return inspect_live_account(gateway, Decimal(0))
        finally:
            _close_gateway(gateway)

    def preview(
        self,
        request: CampaignPreviewRequest,
        *,
        require_flat: bool = False,
    ) -> dict[str, Any]:
        gateway = self.gateway_factory()
        try:
            allocation = self.provider_factory().get()
            campaign = BetaVolumeCampaign.create(
                gateway,
                allocation,
                profile_fingerprint=self.profile_fingerprint,
                target_turnover_quote=request.target_quote,
                round_turnover_quote=request.cycle_volume,
                hold_min_seconds=request.hold_min_seconds,
                hold_max_seconds=request.hold_max_seconds,
                round_gap_min_seconds=request.round_gap_min_seconds,
                round_gap_max_seconds=request.round_gap_max_seconds,
                now_ms=self.now_ms(),
            )
            opening_notional = min(campaign.round_turnover_quote, campaign.target_turnover_quote) / 2
            required = opening_notional / Decimal(campaign.max_auto_leverage) * campaign.margin_buffer
            readiness = inspect_live_account(
                gateway,
                required,
                opening_notional=opening_notional,
                leverage=campaign.leverage,
                max_auto_leverage=campaign.max_auto_leverage,
                margin_buffer=campaign.margin_buffer,
            )
            if require_flat and not _is_flat(readiness):
                raise SafetyError("campaign preview requires flat BTC/ETH positions and no active orders")
            path = self.campaign_store.create(campaign)
            payload = campaign_plan_payload(campaign, path, readiness)
            available = Decimal(str(readiness.get("available_quote") or "0"))
            payload.update(
                {
                    "stop_confirm": campaign_stop_confirmation(campaign),
                    "estimated_cycles": int(
                        (campaign.target_turnover_quote / campaign.round_turnover_quote).to_integral_value(
                            rounding="ROUND_CEILING"
                        )
                    ),
                    "max_supported_turnover_quote": str(
                        available * Decimal(campaign.max_auto_leverage) / campaign.margin_buffer * Decimal(2)
                    ),
                }
            )
            return payload
        finally:
            _close_gateway(gateway)

    def load(self, campaign_id: str) -> BetaVolumeCampaignRecord:
        return self.campaign_store.load(campaign_id)

    def execute(
        self,
        *,
        confirmation: str,
        campaign_id: str | None = None,
        event_sink: CampaignEventSink | None = None,
        stop_requested: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        confirmed_id = campaign_id_from_confirmation(confirmation)
        if campaign_id is not None and campaign_id.lower() != confirmed_id:
            raise SafetyError("campaign ID does not match the exact confirmation")
        record = self.campaign_store.load(confirmed_id)
        require_execution(
            execute=True,
            supplied=confirmation,
            expected=campaign_confirmation(record.campaign),
            mode="live",
            settings=self.profile.settings,
        )
        self.profile.require_maker_execution()
        if record.campaign.profile_fingerprint != self.profile_fingerprint:
            raise SafetyError("campaign was authorized for a different live profile")
        if self.now_ms() >= record.campaign.expires_at_ms:
            raise SafetyError("campaign authorization expired; create a new preview")

        gateway = self.gateway_factory()
        snapshot_gateway: WeexGateway | None = None
        lanes: dict[str, WeexGateway] = {}
        runtime: CampaignRuntime | None = None
        try:
            lanes = {"BTC": gateway.fork(), "ETH": gateway.fork()}
            snapshot_gateway = gateway.fork()
            _require_distinct_gateways(gateway, snapshot_gateway, lanes)
            runtime = self.runtime_factory(snapshot_gateway, self.profile)
            runtime.start()
            return LiveBetaVolumeCampaignService(
                gateway,
                self.provider_factory(),
                self.campaign_store,
                self.child_store,
                profile_fingerprint=self.profile_fingerprint,
                event_sink=event_sink,
                lane_gateways=lanes,
                market_data=runtime,
                order_updates=runtime,
                stop_requested=stop_requested,
                now_ms=self.now_ms,
            ).execute(record.campaign)
        finally:
            if runtime is not None:
                runtime.close()
            if snapshot_gateway is not None:
                _close_gateway(snapshot_gateway)
            for lane in lanes.values():
                _close_gateway(lane)
            _close_gateway(gateway)

    def mark_interrupted_uncertain(self) -> list[str]:
        recovered: list[str] = []
        if not self.paths.campaigns.exists():
            return recovered
        for path in sorted(self.paths.campaigns.glob("wc-*.json")):
            try:
                record = self.campaign_store.load(path.stem)
            except Exception:  # noqa: BLE001 - corrupt journals stay untouched for manual inspection
                continue
            if record.state not in {"executing", "stopping"}:
                continue
            result = {
                "schema_version": 1,
                "kind": "beta_volume_campaign_execution",
                "mode": "live",
                "status": "uncertain",
                "reason": "tui_process_restart",
                "campaign_id": record.campaign.campaign_id,
                "retry_allowed": False,
            }
            self.campaign_store.save(record.campaign, state="uncertain", result=result)
            recovered.append(record.campaign.campaign_id)
        return recovered

    @staticmethod
    def _default_runtime(snapshot_gateway: WeexGateway, profile: LiveProfile) -> CampaignRuntime:
        return WeexCampaignWebSocketRuntime(
            snapshot_gateway,
            profile.settings.require_credentials(),
            proxy_url=profile.proxy_url,
        )


def campaign_stop_confirmation(campaign: BetaVolumeCampaign) -> str:
    return f"STOP WEEX LIVE BETA-CAMPAIGN {campaign.campaign_id.upper()} POST_ONLY"


def _is_flat(readiness: Mapping[str, Any]) -> bool:
    return all(
        int(readiness.get(key, -1)) == 0
        for key in (
            "active_position_count",
            "regular_order_count",
            "trigger_order_count",
        )
    )


def _require_distinct_gateways(
    gateway: WeexGateway,
    snapshot_gateway: WeexGateway,
    lanes: Mapping[str, WeexGateway],
) -> None:
    resources = [gateway, snapshot_gateway, lanes["BTC"], lanes["ETH"]]
    if len({id(resource) for resource in resources}) != 4:
        raise SafetyError("campaign requires four independent WEEX gateway instances")


def _close_gateway(gateway: WeexGateway) -> None:
    close = getattr(gateway, "close", None)
    if callable(close):
        close()
