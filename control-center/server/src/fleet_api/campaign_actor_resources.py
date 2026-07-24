"""Short-lived gateway and stream resources leased by Campaign actors."""

from __future__ import annotations

import threading
from collections.abc import Mapping
from contextlib import ExitStack
from typing import Any

from weex_cli.beta_campaign import BetaVolumeCampaignStore, LiveBetaVolumeCampaignService, live_profile_fingerprint
from weex_cli.beta_volume import BetaVolumePlanStore, LiveBetaVolumeService
from weex_cli.live_websocket import WeexPrivateOrderStream, WeexPublicOrderBookStream

from .campaign_actor_models import CampaignPhaseEnvironment
from .campaign_contracts import CampaignRecord
from .execution_io import NORMAL_IO_PRIORITY, BoundedGateway
from .vault import CredentialMaterial


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
                gateway=gateway,
                instance_id=record.instance_id,
                proxy_key=self._proxy_key(material),
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
        gateway: BoundedGateway,
        instance_id: str,
        proxy_key: str,
    ) -> tuple[Any | None, Any | None]:
        if not self.settings.live_campaign_websockets_enabled or phase not in {"open", "close", "safe_stop"}:
            return None, None
        market_data = leases.enter_context(
            self.market_data_hub.lease(
                proxy_key,
                lambda: _open_public_market_stream(gateway.fork(priority=NORMAL_IO_PRIORITY), profile.proxy_url),
            )
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


class _PublicMarketStream:
    def __init__(self, snapshot_gateway: BoundedGateway, proxy_url: str | None) -> None:
        self._snapshot_gateway = snapshot_gateway
        self._stream = WeexPublicOrderBookStream(snapshot_gateway, proxy_url=proxy_url)
        self._stream.start()

    def order_book(self, symbol: str, limit: int = 5) -> dict[str, Any]:
        return self._stream.order_book(symbol, limit)

    def close(self) -> None:
        self._stream.close()
        self._snapshot_gateway.close()


def _open_public_market_stream(snapshot_gateway: BoundedGateway, proxy_url: str | None) -> _PublicMarketStream:
    return _PublicMarketStream(snapshot_gateway, proxy_url)


def _open_private_order_stream(credentials: Any, proxy_url: str | None) -> WeexPrivateOrderStream:
    stream = WeexPrivateOrderStream(credentials, proxy_url=proxy_url)
    stream.start()
    return stream
