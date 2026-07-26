"""Short-lived gateway and stream resources leased by Campaign actors."""

from __future__ import annotations

import threading
from collections.abc import Mapping
from contextlib import ExitStack
from typing import Any

from weex_cli.control_api.campaigns import (
    BetaVolumeCampaignStore,
    LiveBetaVolumeCampaignService,
    live_profile_fingerprint,
)
from weex_cli.control_api.streams import WeexPrivateOrderStream
from weex_cli.control_api.volume import BetaVolumePlanStore, LiveBetaVolumeService

from fleet_api.auth.vault import CredentialMaterial
from fleet_api.campaigns.actors.campaign_actor_models import CampaignPhaseEnvironment
from fleet_api.campaigns.core.campaign_contracts import CampaignRecord
from fleet_api.execution.runtime.execution_io import BoundedGateway


class CampaignActorResourceMixin:
    def _actor_environment(
        self,
        record: CampaignRecord,
        material: CredentialMaterial,
        stop: threading.Event,
        phase: str,
    ) -> CampaignPhaseEnvironment:
        profile, raw_gateway = self._profile_and_gateway(material)
        gateway = BoundedGateway(raw_gateway, self.io_budget, stop)
        lanes: dict[str, BoundedGateway] = {}
        leases = ExitStack()
        try:
            lanes = {"BTC": gateway.fork(), "ETH": gateway.fork()}
            provider = self.beta_provider_factory()
            event_sink = self._actor_event_sink(record)
            root = self.settings.campaign_data_directory / record.instance_id
            campaign_store = BetaVolumeCampaignStore(root)
            child_store = BetaVolumePlanStore(root / "plans")
            market_data, order_updates = self._phase_streams(
                leases,
                phase=phase,
                profile=profile,
                instance_id=record.instance_id,
                stop=stop,
            )
            shared = dict(
                event_sink=event_sink,
                lane_gateways=lanes,
                market_data=market_data,
                order_updates=order_updates,
                stop_requested=stop.is_set,
            )
            campaign_service = LiveBetaVolumeCampaignService(
                gateway,
                provider,
                campaign_store,
                child_store,
                profile_fingerprint=live_profile_fingerprint(profile),
                **shared,
            )
            volume_service = LiveBetaVolumeService(gateway, provider, child_store, **shared)
            return CampaignPhaseEnvironment(
                campaign_service,
                volume_service,
                lambda: _close_actor_environment(leases, gateway, lanes),
            )
        except Exception:
            leases.close()
            _close_environment(gateway, lanes)
            raise

    def _phase_streams(
        self,
        leases: ExitStack,
        *,
        phase: str,
        profile: Any,
        instance_id: str,
        stop: threading.Event,
    ) -> tuple[Any | None, Any | None]:
        if not self.settings.live_campaign_websockets_enabled or phase not in {"open", "close", "safe_stop"}:
            return None, None
        market_data = (
            self.public_market_snapshot_service.actor_view(
                threading.Event() if phase == "safe_stop" else stop,
                max_wait_seconds=15 if phase == "safe_stop" else 5,
            )
            if self.public_market_snapshot_service.enabled
            else None
        )
        order_updates = leases.enter_context(
            self.private_order_stream_pool.lease(
                instance_id,
                lambda: _open_private_order_stream(profile.settings.require_credentials(), profile.proxy_url),
            )
        )
        return market_data, order_updates


def _close_environment(gateway: BoundedGateway, lanes: Mapping[str, BoundedGateway]) -> None:
    for lane in lanes.values():
        lane.close()
    gateway.close()


def _close_actor_environment(
    leases: ExitStack,
    gateway: BoundedGateway,
    lanes: Mapping[str, BoundedGateway],
) -> None:
    leases.close()
    _close_environment(gateway, lanes)


def _open_private_order_stream(credentials: Any, proxy_url: str | None) -> WeexPrivateOrderStream:
    stream = WeexPrivateOrderStream(credentials, proxy_url=proxy_url)
    stream.start()
    return stream
