from __future__ import annotations

import socket
from pathlib import Path
from typing import Annotated

import typer

from weex_cli import __version__
from weex_cli.cli_support import AppContext, gateway_for, invoke, selected_mode, settings_for
from weex_cli.commands import account, config_cmd, market, order, orders, risk
from weex_cli.output import emit

app = typer.Typer(
    name="weex",
    help="Safety-first WEEX contract CLI. Commands are read-only or dry-run unless explicitly executed.",
    no_args_is_help=True,
    invoke_without_command=True,
)
app.add_typer(market.app, name="market")
app.add_typer(account.app, name="account")
app.add_typer(orders.app, name="orders")
app.add_typer(order.app, name="order")
app.add_typer(risk.app, name="risk")
app.add_typer(config_cmd.app, name="config")


@app.callback()
def main(
    ctx: typer.Context,
    env_file: Annotated[Path | None, typer.Option("--env-file", help="Load credentials from this env file")] = None,
    version: Annotated[bool, typer.Option("--version", is_eager=True)] = False,
) -> None:
    """Configure the CLI context."""
    if version:
        typer.echo(__version__)
        raise typer.Exit()
    ctx.obj = AppContext(env_file=env_file)


@app.command("doctor")
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


if __name__ == "__main__":  # pragma: no cover
    app()
