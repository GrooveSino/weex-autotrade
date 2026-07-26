"""Fleet ASGI composition root. Domain routes live in focused modules."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from weex_cli.control_api.allocation import (
    HttpBetaAllocationProvider as LiveCampaignBetaAllocationProvider,  # noqa: F401
)

from fleet_api.bootstrap.main_bootstrap import build_context, finish_context
from fleet_api.bootstrap.main_campaign_lifecycle import install_campaign_lifecycle
from fleet_api.bootstrap.main_lifecycle import install_application_lifecycle
from fleet_api.bootstrap.main_support import install_projection_support
from fleet_api.config.config import ControlPlaneSettings
from fleet_api.execution import PairAllocationProvider
from fleet_api.market.beta_allocation import HttpBetaAllocationProvider  # noqa: F401
from fleet_api.runtime.telemetry import AccountTelemetryAdapterFactory
from fleet_api.transport.http.main_http import install_http_support
from fleet_api.transport.routes.main_routes_accounts import register_account_routes
from fleet_api.transport.routes.main_routes_bound import register_bound_strategy_routes
from fleet_api.transport.routes.main_routes_campaign import register_campaign_routes
from fleet_api.transport.routes.main_routes_health import register_health_routes
from fleet_api.transport.routes.main_routes_instances import register_instance_routes
from fleet_api.transport.routes.main_routes_monitor import register_strategy_monitor_routes
from fleet_api.transport.routes.main_routes_volume import register_trade_volume_report_routes


def create_app(
    settings: ControlPlaneSettings | None = None,
    adapter_factory: AccountTelemetryAdapterFactory | None = None,
    allocation_provider: PairAllocationProvider | None = None,
    *,
    require_command_id: bool = False,
) -> FastAPI:
    selected = settings or ControlPlaneSettings.load()
    ctx = build_context(selected, require_command_id=require_command_id)
    install_campaign_lifecycle(ctx)
    finish_context(ctx, adapter_factory, allocation_provider)
    install_projection_support(ctx)
    install_application_lifecycle(ctx)
    app = FastAPI(
        title="WEEX Fleet Control Plane",
        version="0.1.0",
        description=(
            "Private executor for WEEX Fleet. Live bound-strategy execution remains "
            "confirmation-gated, idempotent, POST_ONLY, and automatically read-only recovered; "
            "the public API proxy never submits exchange commands itself."
        ),
        lifespan=ctx.lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(selected.cors_origins),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["Content-Type", "X-Fleet-Command-Id"],
    )
    install_http_support(app, ctx)
    register_health_routes(app, ctx)
    register_account_routes(app, ctx)
    register_bound_strategy_routes(app, ctx)
    register_strategy_monitor_routes(app, ctx)
    register_campaign_routes(app, ctx)
    register_instance_routes(app, ctx)
    register_trade_volume_report_routes(app, ctx)
    return app


def run() -> None:
    import uvicorn

    uvicorn.run(create_app(), host="127.0.0.1", port=8000, reload=False)
