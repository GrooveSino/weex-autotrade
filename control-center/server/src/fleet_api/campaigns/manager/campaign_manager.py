from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from threading import RLock
from typing import Any

from weex_cli.beta_allocation import HttpBetaAllocationProvider

from fleet_api.auth.vault import CredentialVault
from fleet_api.campaigns.core.campaign_contracts import CampaignJournal, CampaignRecord, _AccountLease
from fleet_api.campaigns.core.campaign_events import _view
from fleet_api.campaigns.manager.campaign_manager_actor import CampaignActorRuntimeMixin
from fleet_api.campaigns.manager.campaign_manager_bound import CampaignBoundStrategyMixin
from fleet_api.campaigns.manager.campaign_manager_cleanup import CampaignCleanupMixin
from fleet_api.campaigns.manager.campaign_manager_commands import CampaignCommandMixin
from fleet_api.campaigns.manager.campaign_manager_restart import CampaignRestartMixin
from fleet_api.campaigns.manager.campaign_manager_worker import CampaignWorkerRuntimeMixin
from fleet_api.config.config import ControlPlaneSettings
from fleet_api.execution.resources.fleet_write_coordinator import FleetWriteCoordinator
from fleet_api.execution.resources.market_data_hub import PublicMarketSnapshotService
from fleet_api.execution.resources.private_order_stream_pool import PrivateOrderStreamPool
from fleet_api.execution.runtime.execution_capacity import ExecutionCapacity, ExecutionCapacitySnapshot
from fleet_api.execution.runtime.execution_io import ExecutionIoBudget, ExecutionIoSnapshot
from fleet_api.execution.runtime.execution_phase_pacer import ExecutionPhasePacer
from fleet_api.models import (
    BetaCampaignEvent,
    BetaCampaignView,
)


class CampaignWorkerManager(
    CampaignRestartMixin,
    CampaignCommandMixin,
    CampaignBoundStrategyMixin,
    CampaignCleanupMixin,
    CampaignActorRuntimeMixin,
    CampaignWorkerRuntimeMixin,
):
    def __init__(
        self,
        settings: ControlPlaneSettings,
        vault: CredentialVault,
        journal: CampaignJournal,
        beta_provider_factory: Callable[[], HttpBetaAllocationProvider],
        *,
        on_change: Callable[[str], None] | None = None,
        on_progress: Callable[[str, Mapping[str, Any]], None] | None = None,
        on_execution_claim: Callable[[CampaignRecord, int], None] | None = None,
        executor_generation: str = "local",
    ) -> None:
        self.settings = settings
        self.vault = vault
        self.journal = journal
        self.beta_provider_factory = beta_provider_factory
        self.on_change = on_change or (lambda _instance_id: None)
        self.on_progress = on_progress or (lambda _instance_id, _event: None)
        self.on_execution_claim = on_execution_claim or (lambda _record, _started_at_ms: None)
        self.executor_generation = executor_generation
        self.capacity = ExecutionCapacity(
            max_active_executions=settings.max_active_executions,
            max_normal_phases=settings.normal_phase_max_concurrency,
            phase_start_rate_per_second=settings.normal_phase_starts_per_second,
            per_proxy_gap_seconds=settings.normal_phase_proxy_gap_seconds,
            stable_jitter_seconds=settings.execution_phase_jitter_seconds,
        )
        self.phase_pacer = ExecutionPhasePacer(capacity=self.capacity)
        self.io_budget = ExecutionIoBudget(
            max_normal=settings.execution_io_normal_capacity,
            max_emergency=settings.execution_io_emergency_capacity,
        )
        self.write_coordinator = FleetWriteCoordinator()
        self.public_market_snapshot_service = PublicMarketSnapshotService(
            enabled=settings.adapter == "weex-live" and settings.live_campaign_websockets_enabled,
            request_timeout_ms=settings.weex_request_timeout_ms,
            proxy_url=settings.shared_market_data_proxy_url,
        )
        self.public_market_snapshot_service.start()
        self.private_order_stream_pool = PrivateOrderStreamPool()
        legacy_workers = 1 if settings.async_actor_runtime_enabled else settings.live_campaign_worker_count
        self._executor = ThreadPoolExecutor(max_workers=legacy_workers, thread_name_prefix="weex-campaign")
        self._stops: dict[str, threading.Event] = {}
        self._futures: dict[str, Future[None]] = {}
        self._leases: dict[str, _AccountLease] = {}
        self._starting: set[str] = set()
        self._cleaning: set[str] = set()
        self._closing = False
        self._lock = RLock()
        self._create_actor_runtime()

    def get(self, instance_id: str, campaign_id: str) -> BetaCampaignView:
        return _view(self._require_record(instance_id, campaign_id))

    def list(self, instance_id: str) -> list[BetaCampaignView]:
        return [_view(record, include_events=False) for record in self.journal.list_for_instance(instance_id)]

    def events(self, instance_id: str, campaign_id: str) -> list[BetaCampaignEvent]:
        record = self._require_record(instance_id, campaign_id)
        return [BetaCampaignEvent.model_validate(event) for event in record.events]

    def public_snapshot(self) -> list[dict[str, Any]]:
        if not hasattr(self.journal, "list_all"):
            return []
        return [
            _view(record, include_events=False).model_dump(mode="json", by_alias=True)
            for record in self.journal.list_all()
        ]  # type: ignore[attr-defined]

    def active_worker_count(self) -> int:
        """Return work currently owned by this process, not executor capacity."""
        with self._lock:
            active_futures = {campaign_id for campaign_id, future in self._futures.items() if not future.done()}
            active_actors = {campaign_id for campaign_id, future in self._actor_futures.items() if not future.done()}
            return len(self._starting | active_futures | active_actors)

    def capacity_snapshot(self) -> ExecutionCapacitySnapshot:
        return self.phase_pacer.snapshot()

    def io_snapshot(self) -> ExecutionIoSnapshot:
        return self.io_budget.snapshot()

    def has_active_worker(self, instance_id: str) -> bool:
        """Report whether this process still owns executable work for an account."""
        with self._lock:
            campaign_ids = set(self._starting)
            campaign_ids.update(campaign_id for campaign_id, future in self._futures.items() if not future.done())
            campaign_ids.update(campaign_id for campaign_id, future in self._actor_futures.items() if not future.done())
        return any(
            (record := self.journal.get(campaign_id)) is not None and record.instance_id == instance_id
            for campaign_id in campaign_ids
        )

    def close(self) -> None:
        with self._lock:
            self._closing = True
            for stop in self._stops.values():
                stop.set()
        self._actor_runtime.close()
        self._executor.shutdown(wait=True, cancel_futures=False)
        self.close_boundary_reader()
        self.private_order_stream_pool.close()
        self.public_market_snapshot_service.close()
        self.write_coordinator.close()
        self.journal.close()
