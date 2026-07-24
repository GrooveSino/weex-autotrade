"""Create Fleet dependencies without registering HTTP routes."""

from __future__ import annotations

import asyncio
import os
import time
from uuid import uuid4

from weex_cli.beta_allocation import HttpBetaAllocationProvider as LiveCampaignBetaAllocationProvider

from .beta_allocation import HttpBetaAllocationProvider
from .beta_source import BetaSourceRuntime, InMemoryBetaSourceStore, SQLiteBetaSourceStore
from .campaigns import CampaignWorkerManager, InMemoryCampaignJournal, SQLiteCampaignJournal
from .command_ledger import CommandReceiptLedger
from .config import ControlPlaneSettings
from .events import InstanceEventBroker
from .execution import (
    InMemoryExecutionJournal,
    MockPairedExecutionAdapterFactory,
    PairAllocationProvider,
    PairedCycleCoordinator,
    SQLiteExecutionJournal,
)
from .main_context import FleetAppContext
from .models import BetaSourceSettings
from .repository import InMemoryAccountRepository, SQLiteAccountRepository
from .runtime import AccountRuntimeManager
from .seed import ensure_mock_volume_baselines, seed_mock_instances
from .service import FleetControlService
from .strategy_monitor import StrategyMonitorService
from .strategy_run_lifecycle import StrategyRunLifecycleService
from .telemetry import AccountTelemetryAdapterFactory, MockAccountTelemetryAdapterFactory
from .trade_history_scheduler import TradeHistorySyncScheduler
from .vault import EncryptedSQLiteCredentialVault, EphemeralCredentialVault
from .volume_history import InMemoryTradeVolumeLedger, SessionVolumeService, SQLiteTradeVolumeLedger
from .weex_readonly import WeexReadonlyAccountTelemetryAdapterFactory


def _beta_provider(source: BetaSourceSettings) -> HttpBetaAllocationProvider:
    # Preserve the long-standing facade seam used by fake providers.
    from . import main as main_api

    return main_api.HttpBetaAllocationProvider(
        source.url,
        timeout_seconds=source.timeout_seconds,
        cache_seconds=source.refresh_interval_seconds,
        network_on_demand=not source.background_refresh_enabled,
    )


def build_context(settings: ControlPlaneSettings, *, require_command_id: bool) -> FleetAppContext:
    ctx = FleetAppContext(selected=settings, require_command_id=require_command_id)
    if settings.storage == "sqlite":
        assert settings.master_key is not None
        ctx.repository = SQLiteAccountRepository(settings.sqlite_path)
        ctx.vault = EncryptedSQLiteCredentialVault(settings.sqlite_path, settings.master_key)
        try:
            ctx.vault.verify_all()
            ctx.volume_ledger = SQLiteTradeVolumeLedger(settings.sqlite_path)
            ctx.execution_journal = SQLiteExecutionJournal(settings.sqlite_path)
        except Exception:
            ctx.vault.close()
            ctx.repository.close()
            raise
        ctx.beta_source_store = SQLiteBetaSourceStore(settings.sqlite_path)
    else:
        ctx.repository = InMemoryAccountRepository()
        ctx.vault = EphemeralCredentialVault()
        ctx.volume_ledger = InMemoryTradeVolumeLedger()
        ctx.execution_journal = InMemoryExecutionJournal()
        ctx.beta_source_store = InMemoryBetaSourceStore()
    ctx.command_ledger = CommandReceiptLedger(settings.sqlite_path if settings.storage == "sqlite" else None)
    ctx.had_persisted_instances = bool(ctx.repository.list())
    ctx.service = FleetControlService(
        ctx.repository,
        ctx.vault,
        adapter=settings.adapter,
        mock_cycle_total_quote=settings.mock_cycle_total_quote,
    )
    ctx.executor_generation = uuid4().hex
    ctx.executor_release_id = os.environ.get("FLEET_EXECUTOR_RELEASE_ID", "dev").strip() or "dev"
    ctx.broker = InstanceEventBroker(ctx.executor_generation)
    ctx.execution_journal.recover_incomplete()
    ctx.beta_source_runtime = BetaSourceRuntime(
        ctx.beta_source_store,
        BetaSourceSettings(
            url=settings.beta_ratio_url,
            timeout_seconds=settings.beta_ratio_timeout_seconds,
            refresh_interval_seconds=settings.beta_refresh_interval_seconds,
            background_refresh_enabled=settings.beta_background_refresh_enabled,
            updated_at_ms=time.time_ns() // 1_000_000,
        ),
        provider_factory=_beta_provider,
    )
    ctx.campaign_journal = (
        SQLiteCampaignJournal(settings.sqlite_path) if settings.storage == "sqlite" else InMemoryCampaignJournal()
    )
    ctx.event_loop: asyncio.AbstractEventLoop | None = None
    ctx.session_finalizations: set[str] = set()
    ctx.session_finalization_tasks: set[asyncio.Task[None]] = set()
    ctx.execution_coordinator = None
    ctx.selected_allocation_provider = None
    return ctx


def finish_context(
    ctx: FleetAppContext,
    adapter_factory: AccountTelemetryAdapterFactory | None,
    allocation_provider: PairAllocationProvider | None,
) -> None:
    settings = ctx.selected
    ctx.campaign_manager = CampaignWorkerManager(
        settings,
        ctx.vault,
        ctx.campaign_journal,
        lambda: _live_campaign_provider(ctx),
        on_change=ctx.notify_campaign_change,
        on_execution_claim=ctx.establish_bound_strategy_session,
        executor_generation=ctx.executor_generation,
    )
    ctx.campaign_manager.recover()
    ctx.campaign_manager.invalidate_stale_planned_bound_strategy_previews(
        {item.id: item.strategy for item in ctx.service.list_instances()},
        reason="executor_startup_strategy_snapshot_stale",
    )
    if settings.adapter == "mock":
        ctx.selected_allocation_provider = allocation_provider or ctx.beta_source_runtime
        ctx.execution_coordinator = PairedCycleCoordinator(
            ctx.execution_journal,
            ctx.volume_ledger,
            ctx.selected_allocation_provider,
            MockPairedExecutionAdapterFactory(),
            total_quote=settings.mock_cycle_total_quote,
        )
    if adapter_factory is not None:
        telemetry_factory = adapter_factory
    elif settings.adapter == "mock":
        telemetry_factory = MockAccountTelemetryAdapterFactory()
    else:
        telemetry_factory = WeexReadonlyAccountTelemetryAdapterFactory(
            ctx.volume_ledger,
            request_timeout_ms=settings.weex_request_timeout_ms,
            history_lookback_days=settings.weex_history_lookback_days,
            history_pages_per_poll=settings.weex_history_pages_per_poll,
        )
    ctx.runtime = AccountRuntimeManager(
        ctx.service,
        telemetry_factory,
        ctx.volume_ledger,
        ctx.execution_coordinator,
        max_parallel_polls=settings.max_parallel_polls,
        poll_timeout_seconds=settings.account_poll_timeout_seconds,
    )
    ctx.session_volume = SessionVolumeService(ctx.volume_ledger)
    ctx.strategy_run_lifecycle = StrategyRunLifecycleService(
        ctx.volume_ledger,
        ctx.session_volume,
        ctx.runtime,
        ctx.campaign_manager,
        ctx.campaign_journal,
        ctx.service,
        ctx.vault,
    )
    ctx.trade_history_scheduler = TradeHistorySyncScheduler(
        ctx.service,
        ctx.runtime,
        ctx.volume_ledger,
        is_active=lambda instance: _needs_history_sync(ctx, instance),
        active_fallback_seconds=settings.weex_history_active_fallback_seconds,
        max_concurrent_requests=settings.weex_history_max_concurrency,
    )
    ctx.strategy_monitor = StrategyMonitorService(ctx.campaign_journal, ctx.volume_ledger, ctx.executor_generation)
    ctx.strategy_monitor.rebuild_all()
    if ctx.had_persisted_instances:
        ensure_mock_volume_baselines(ctx.repository.list(), ctx.volume_ledger, time.time_ns() // 1_000_000)
        ctx.service.reconcile_after_restart()
    elif settings.seed_demo_data and settings.adapter == "mock":
        seed_mock_instances(
            ctx.repository,
            ctx.volume_ledger,
            time.time_ns() // 1_000_000,
            settings.mock_cycle_total_quote,
        )


def _live_campaign_provider(ctx: FleetAppContext) -> LiveCampaignBetaAllocationProvider:
    # Keep the legacy ``fleet_api.main`` patch seam for fake-client tests and
    # external embedders while keeping composition in this focused module.
    from . import main as main_api

    return main_api.LiveCampaignBetaAllocationProvider(
        ctx.beta_source_runtime.settings.url,
        timeout_seconds=ctx.beta_source_runtime.settings.timeout_seconds,
        allow_low_confidence=True,
    )


def _needs_history_sync(ctx: FleetAppContext, instance) -> bool:
    if instance.mode.value != "live":
        return instance.status.value == "running"
    lifecycle = ctx.strategy_run_lifecycle.projection(instance.id, instance.mode.value)
    return lifecycle.state in {"running", "stopping", "cleanup_required"}
