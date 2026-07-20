from __future__ import annotations

import socket
from pathlib import Path
from typing import Annotated

import typer

from weex_cli import __version__
from weex_cli.cli_support import AppContext, gateway_for, invoke, selected_mode, settings_for
from weex_cli.commands import account, config_cmd, home, live, maker_cli, market, order, orders, risk, trades, volume
from weex_cli.i18n import (
    current_language,
    install_typer_i18n,
    localize_typer_app,
    set_language,
    text,
)
from weex_cli.output import emit

install_typer_i18n()

app = typer.Typer(
    name="weex",
    help="Operate WEEX safely: inspect state, run Demo Maker workflows, and review activity.",
    no_args_is_help=True,
    invoke_without_command=True,
    add_completion=False,
    rich_markup_mode="rich",
    context_settings={"help_option_names": ["-h", "--help"]},
    pretty_exceptions_show_locals=False,
)

advanced = typer.Typer(help="Low-level exchange and maintenance commands.", no_args_is_help=True)
advanced.add_typer(market.app, name="market")
advanced.add_typer(account.app, name="account")
advanced.add_typer(orders.app, name="orders")
advanced.add_typer(order.app, name="order")
advanced.add_typer(risk.app, name="risk")
advanced.add_typer(trades.app, name="trades")
advanced.add_typer(volume.app, name="volume")
advanced.add_typer(config_cmd.app, name="config")

app.command("status", rich_help_panel="Daily workflow")(home.status)
app.add_typer(maker_cli.app, name="maker", rich_help_panel="Daily workflow")
app.add_typer(live.app, name="live", rich_help_panel="Daily workflow")
app.command("activity", rich_help_panel="Daily workflow")(home.activity)
app.add_typer(advanced, name="advanced", rich_help_panel="Maintenance")

# Preserve existing automation paths without crowding the human-first help screen.
app.add_typer(market.app, name="market", hidden=True)
app.add_typer(account.app, name="account", hidden=True)
app.add_typer(orders.app, name="orders", hidden=True)
app.add_typer(order.app, name="order", hidden=True)
app.add_typer(risk.app, name="risk", hidden=True)
app.add_typer(trades.app, name="trades", hidden=True)
app.add_typer(volume.app, name="volume", hidden=True)
app.add_typer(config_cmd.app, name="config", hidden=True)


@app.callback()
def main(
    ctx: typer.Context,
    env_file: Annotated[Path | None, typer.Option("--env-file", help="Load credentials from this env file")] = None,
    profile: Annotated[
        Path | None,
        typer.Option("--profile", help="Load an explicit project-local TOML live profile"),
    ] = None,
    language: Annotated[
        str,
        typer.Option("--lang", help=text("界面语言：zh 或 en", "Interface language: zh or en")),
    ] = current_language(),
    version: Annotated[bool, typer.Option("--version", is_eager=True)] = False,
) -> None:
    """Configure the CLI context."""
    try:
        selected_language = set_language(language)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--lang") from exc
    if version:
        typer.echo(__version__)
        raise typer.Exit()
    if env_file is not None and profile is not None:
        raise typer.BadParameter("--env-file and --profile cannot be used together")
    ctx.obj = AppContext(env_file=env_file, profile_file=profile, language=selected_language)


@app.command("doctor", rich_help_panel="Maintenance")
def doctor(
    ctx: typer.Context,
    symbol: str = typer.Option("BTC"),
    mode: str | None = typer.Option(None, help="Private check mode: demo or live"),
    private: bool = typer.Option(False, help="Also perform a read-only authenticated balance request"),
    network: bool = typer.Option(True, "--network/--no-network"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    settings = settings_for(ctx)
    checks: dict[str, object] = {
        "version": __version__,
        "credentials_configured": settings.credentials.configured,
        "live_trading_enabled": settings.live_trading_enabled,
        "default_mode": settings.default_mode,
        "env_file": settings.env_file,
        "dns": {},
    }
    for host in ("api-spot.weex.com", "api-contract.weex.com"):
        try:
            addresses = sorted({row[4][0] for row in socket.getaddrinfo(host, 443)})
            checks["dns"][host] = {"ok": True, "addresses": addresses}  # type: ignore[index]
        except OSError as exc:
            checks["dns"][host] = {"ok": False, "error": str(exc)}  # type: ignore[index]
    if network:
        try:
            ticker = gateway_for(ctx, private=False).ticker(symbol)
            checks["public_api"] = {"ok": True, "symbol": ticker.get("symbol"), "last": ticker.get("last")}
        except Exception as exc:  # noqa: BLE001 - doctor reports rather than raises each check
            checks["public_api"] = {"ok": False, "error": str(exc)}
    if private:
        selected = selected_mode(ctx, mode)
        checks["private_api"] = invoke(
            lambda: {"ok": True, "mode": selected, "balance": gateway_for(ctx).balance(selected)}
        )
    emit(checks, json_output=json_output)


localize_typer_app(app)


if __name__ == "__main__":  # pragma: no cover
    app()
