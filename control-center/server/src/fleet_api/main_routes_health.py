from __future__ import annotations

from fastapi import HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from .executor_process_metrics import process_snapshot
from .execution import AllocationUnavailable
from .models import (
    BetaMarketSnapshot,
    BetaSourceSettings,
    BetaSourceSettingsUpdate,
    ExecutionCapacityResponse,
    HealthResponse,
    SchedulerMetrics,
)
from .service import FleetError, TelemetryUnavailable

from fastapi import FastAPI
from .main_context import FleetAppContext


def register_health_routes(app: FastAPI, ctx: FleetAppContext) -> None:
    selected = ctx.selected
    service = ctx.service
    repository = ctx.repository
    vault = ctx.vault
    volume_ledger = ctx.volume_ledger
    execution_journal = ctx.execution_journal
    execution_coordinator = ctx.execution_coordinator
    selected_allocation_provider = ctx.selected_allocation_provider
    runtime = ctx.runtime
    beta_source_runtime = ctx.beta_source_runtime
    campaign_journal = ctx.campaign_journal
    campaign_manager = ctx.campaign_manager
    app_state_campaign_manager = ctx.campaign_manager
    broker = ctx.broker
    session_volume = ctx.session_volume
    strategy_monitor = ctx.strategy_monitor
    command_ledger = ctx.command_ledger
    executor_generation = ctx.executor_generation
    executor_release_id = ctx.executor_release_id
    latest_bound_record = ctx.latest_bound_record
    finalize_bound_strategy_session = ctx.finalize_bound_strategy_session
    schedule_session_finalization = ctx.schedule_session_finalization
    notify_campaign_change = ctx.notify_campaign_change
    establish_bound_strategy_session = ctx.establish_bound_strategy_session
    publish_snapshot = ctx.publish_snapshot
    refresh_beta_state = ctx.refresh_beta_state
    projected_instances = ctx.projected_instances
    combined_log_updates = ctx.combined_log_updates
    strategy_run_plan = ctx.strategy_run_plan
    require_command_id = ctx.require_command_id

    def capacity_details():  # type: ignore[no-untyped-def]
        capacity = campaign_manager.capacity_snapshot()
        io = campaign_manager.io_snapshot()
        writes = campaign_manager.write_coordinator.snapshot()
        actors = campaign_manager.actor_runtime_snapshot()
        market_data, private_orders = campaign_manager.connection_snapshots()
        history = ctx.trade_history_scheduler.metrics()
        process = process_snapshot()
        return capacity, io, writes, actors, market_data, private_orders, history, process

    @app.exception_handler(FleetError)
    async def fleet_error_handler(_request: Request, exc: FleetError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)})

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
        redacted = [{"loc": list(error["loc"]), "msg": error["msg"], "type": error["type"]} for error in exc.errors()]
        return JSONResponse(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, content={"detail": redacted})

    @app.get("/api/v1/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        journal_metrics = campaign_journal.monitor_metrics()
        stream_metrics = strategy_monitor.metrics()
        capacity, io, writes, actors, market_data, private_orders, history, process = capacity_details()
        ledger_lag = 0
        for record in campaign_journal.list_all():
            session_id = record.metadata.get("session_id")
            if not isinstance(session_id, str) or not session_id:
                continue
            try:
                session = volume_ledger.session_projection(session_id)
            except KeyError:
                continue
            if session and session.get("pending_sync"):
                ledger_lag += 1
        return HealthResponse(
            status="degraded" if int(journal_metrics.get("projection_lag") or 0) else "ok",
            adapter=selected.adapter,
            storage=selected.storage,
            live_trading_enabled=selected.live_trading_enabled,
            execution_enabled=selected.adapter == "mock",
            live_campaigns_enabled=selected.adapter == "weex-live" and selected.live_campaigns_enabled,
            bound_strategy_execution_enabled=(
                selected.adapter == "weex-live" and selected.live_campaigns_enabled and selected.live_trading_enabled
            ),
            live_campaign_active_worker_count=campaign_manager.active_worker_count(),
            live_campaign_worker_count=(selected.live_campaign_worker_count if selected.adapter == "weex-live" else 0),
            api_release_id=executor_release_id,
            executor_connected=True,
            executor_generation=executor_generation,
            monitor_projection_lag=int(journal_metrics.get("projection_lag") or 0),
            monitor_ledger_lag=ledger_lag,
            monitor_sse_subscriber_count=stream_metrics["subscriber_count"],
            monitor_sse_reset_count=stream_metrics["reset_count"],
            monitor_transaction_failure_count=int(journal_metrics.get("transaction_failures") or 0),
            monitor_last_event_at_ms=(
                None
                if journal_metrics.get("last_event_at_ms") is None
                else int(journal_metrics["last_event_at_ms"])
            ),
            active_execution_capacity=capacity.active_executions,
            max_execution_capacity=capacity.max_active_executions,
            active_normal_phase_capacity=capacity.active_normal_phases,
            max_normal_phase_capacity=capacity.max_normal_phases,
            queued_normal_phase_count=capacity.queued_normal_phases,
            capacity_revision=capacity.revision,
            active_normal_io=io.active_normal,
            max_normal_io=io.max_normal,
            active_emergency_io=io.active_emergency,
            max_emergency_io=io.max_emergency,
            active_proxy_phase_partitions=capacity.active_proxy_partitions,
            queued_proxy_limited_phase_count=capacity.queued_proxy_limited_phases,
            normal_phase_queue_p50_ms=capacity.phase_queue_p50_ms,
            normal_phase_queue_p95_ms=capacity.phase_queue_p95_ms,
            sqlite_write_queue_critical=writes.queued_critical,
            sqlite_write_queue_low_priority=writes.queued_low_priority,
            sqlite_write_p95_ms=writes.p95_latency_ms,
            actor_count=actors.actor_count,
            event_loop_delay_p99_ms=actors.event_loop_delay_p99_ms,
            open_file_descriptors=process.open_file_descriptors,
            rss_bytes=process.rss_bytes,
            market_data_active_leases=market_data.active_leases,
            market_data_shared_connections=market_data.shared_connections,
            market_data_idle_connections=market_data.idle_connections,
            private_order_stream_active_leases=private_orders.active_leases,
            private_order_streams=private_orders.open_streams,
            history_sync_queued=history.queued,
            history_sync_running=history.running,
        )

    @app.get("/_internal/executor-health", response_model=HealthResponse, include_in_schema=False)
    def executor_health() -> HealthResponse:
        return health()

    @app.get("/api/v1/executor/capacity", response_model=ExecutionCapacityResponse)
    def execution_capacity() -> ExecutionCapacityResponse:
        capacity, io, writes, actors, market_data, private_orders, history, process = capacity_details()
        return ExecutionCapacityResponse(
            active_executions=capacity.active_executions,
            max_active_executions=capacity.max_active_executions,
            active_normal_phases=capacity.active_normal_phases,
            max_normal_phases=capacity.max_normal_phases,
            queued_normal_phases=capacity.queued_normal_phases,
            phase_start_rate_per_second=capacity.phase_start_rate_per_second,
            per_proxy_gap_seconds=capacity.per_proxy_gap_seconds,
            revision=capacity.revision,
            active_normal_io=io.active_normal,
            max_normal_io=io.max_normal,
            active_emergency_io=io.active_emergency,
            max_emergency_io=io.max_emergency,
            active_proxy_phase_partitions=capacity.active_proxy_partitions,
            queued_proxy_limited_phases=capacity.queued_proxy_limited_phases,
            phase_queue_p50_ms=capacity.phase_queue_p50_ms,
            phase_queue_p95_ms=capacity.phase_queue_p95_ms,
            sqlite_write_queue_critical=writes.queued_critical,
            sqlite_write_queue_low_priority=writes.queued_low_priority,
            sqlite_write_p95_ms=writes.p95_latency_ms,
            actor_count=actors.actor_count,
            event_loop_delay_p99_ms=actors.event_loop_delay_p99_ms,
            open_file_descriptors=process.open_file_descriptors,
            rss_bytes=process.rss_bytes,
            market_data_active_leases=market_data.active_leases,
            market_data_shared_connections=market_data.shared_connections,
            market_data_idle_connections=market_data.idle_connections,
            private_order_stream_active_leases=private_orders.active_leases,
            private_order_streams=private_orders.open_streams,
            history_sync_queued=history.queued,
            history_sync_running=history.running,
        )

    @app.get("/api/v1/commands/{command_id}", response_model=dict[str, str])
    def command_status(command_id: str, request: Request) -> dict[str, str]:
        if not command_id or len(command_id) > 128:
            raise HTTPException(status_code=400, detail="invalid command id")
        owner = request.headers.get("X-Fleet-User", "").strip()
        receipt = command_ledger.get(f"{owner}:{command_id}" if owner else command_id)
        if receipt is None:
            raise HTTPException(status_code=404, detail="command not found")
        return {"commandId": receipt.command_id, "status": receipt.status}

    @app.get("/api/v1/runtime/metrics", response_model=SchedulerMetrics)
    def runtime_metrics() -> SchedulerMetrics:
        return runtime.metrics()

    @app.get("/api/v1/beta", response_model=BetaMarketSnapshot)
    async def beta_snapshot() -> BetaMarketSnapshot:
        try:
            return await app.state.beta_market_provider.market_snapshot()
        except AllocationUnavailable as exc:
            raise TelemetryUnavailable(f"beta snapshot unavailable: {exc.reason_code}") from None

    @app.get("/api/v1/beta/source", response_model=BetaSourceSettings)
    def beta_source_settings() -> BetaSourceSettings:
        return beta_source_runtime.settings

    @app.patch("/api/v1/beta/source", response_model=BetaSourceSettings)
    async def update_beta_source_settings(payload: BetaSourceSettingsUpdate) -> BetaSourceSettings:
        updated = await beta_source_runtime.update(payload)
        # The new source is used immediately for telemetry and only for new
        # Live previews; active executions retain their frozen plan snapshot.
        await refresh_beta_state()
        await publish_snapshot()
        return updated
