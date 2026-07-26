"""Campaign execution service composed from focused lifecycle mixins."""

from __future__ import annotations

import random
import time
from collections.abc import Callable, Mapping
from typing import Any

from weex_cli.beta_campaign.allocation import HttpBetaAllocationProvider
from weex_cli.beta_volume import BetaVolumePlan, BetaVolumePlanStore, PhaseWaiter
from weex_cli.exchange.rest.gateway import WeexGateway

from ..model import BetaVolumeCampaign
from ..store import BetaVolumeCampaignStore
from .execution import _CampaignExecutionMixin
from .results import _CampaignResultMixin
from .support import _CampaignSupportMixin

ChildExecutor = Callable[[BetaVolumePlan], dict[str, Any]]
EventSink = Callable[[Mapping[str, Any]], None]


class LiveBetaVolumeCampaignService(_CampaignExecutionMixin, _CampaignSupportMixin, _CampaignResultMixin):
    def __init__(
        self,
        gateway: WeexGateway,
        provider: HttpBetaAllocationProvider,
        campaign_store: BetaVolumeCampaignStore,
        child_store: BetaVolumePlanStore,
        *,
        profile_fingerprint: str,
        child_executor: ChildExecutor | None = None,
        event_sink: EventSink | None = None,
        lane_gateways: Mapping[str, WeexGateway] | None = None,
        market_data: Any | None = None,
        order_updates: Any | None = None,
        stop_requested: Callable[[], bool] | None = None,
        phase_waiter: PhaseWaiter | None = None,
        now_ms: Callable[[], int] = lambda: int(time.time() * 1000),
        sleep: Callable[[float], None] = time.sleep,
        uniform: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self.gateway = gateway
        self.provider = provider
        self.campaign_store = campaign_store
        self.child_store = child_store
        self.profile_fingerprint = profile_fingerprint
        self.event_sink = event_sink
        self.lane_gateways = lane_gateways
        self.market_data = market_data
        self.order_updates = order_updates
        self.stop_requested = stop_requested or (lambda: False)
        self.phase_waiter = phase_waiter
        self.now_ms = now_ms
        self.sleep = sleep
        self.uniform = uniform
        self.current_campaign: BetaVolumeCampaign | None = None
        self.child_executor = child_executor or self._execute_child
