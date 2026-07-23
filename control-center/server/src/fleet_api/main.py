"""Fleet ASGI composition root. Domain routes live in focused modules."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from weex_cli.beta_allocation import HttpBetaAllocationProvider as LiveCampaignBetaAllocationProvider

from .beta_allocation import HttpBetaAllocationProvider
from .config import ControlPlaneSettings
from .main_bootstrap import build_context, finish_context
from .main_campaign_lifecycle import install_campaign_lifecycle
from .main_http import install_http_support
from .main_lifecycle import install_application_lifecycle
from .main_routes_accounts import register_account_routes
from .main_routes_bound import register_bound_strategy_routes
from .main_routes_campaign import register_campaign_routes
from .main_routes_health import register_health_routes
from .main_routes_instances import register_instance_routes
from .main_routes_monitor import register_strategy_monitor_routes
from .main_support import install_projection_support
from .telemetry import AccountTelemetryAdapterFactory
from .execution import PairAllocationProvider


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
            "confirmation-gated, idempotent, POST_ONLY, and subject to manual reconciliation; "
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
    return app


def run() -> None:
    import uvicorn

    uvicorn.run(create_app(), host="127.0.0.1", port=8000, reload=False)
