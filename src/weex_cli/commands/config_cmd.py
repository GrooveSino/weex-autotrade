from __future__ import annotations

import typer

from weex_cli.cli_support import settings_for
from weex_cli.output import emit

app = typer.Typer(help="Inspect local CLI configuration.")


@app.command("show")
def show_config(ctx: typer.Context, json_output: bool = typer.Option(False, "--json")) -> None:
    settings = settings_for(ctx)
    emit(
        {
            "default_mode": settings.default_mode,
            "live_trading_enabled": settings.live_trading_enabled,
            "timeout_ms": settings.timeout_ms,
            "enable_rate_limit": settings.enable_rate_limit,
            "credentials_configured": settings.credentials.configured,
            "env_file": settings.env_file,
        },
        json_output=json_output,
    )
