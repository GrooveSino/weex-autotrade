from __future__ import annotations

from fastapi import HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from .execution import AllocationUnavailable
from .models import BetaMarketSnapshot, BetaSourceSettings, BetaSourceSettingsUpdate, HealthResponse, SchedulerMetrics
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
        )

    @app.get("/_internal/executor-health", response_model=HealthResponse, include_in_schema=False)
    def executor_health() -> HealthResponse:
        return health()

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
