from __future__ import annotations

import typer

from weex_cli.cli_support import gateway_for, invoke
from weex_cli.output import emit

app = typer.Typer(help="Read public WEEX contract market data.")


@app.command("ticker")
def ticker(
    ctx: typer.Context,
    symbol: str = typer.Argument(..., help="BTC, BTCUSDT, or BTC/USDT:USDT"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    payload = invoke(lambda: gateway_for(ctx, private=False).ticker(symbol))
    emit(payload, json_output=json_output)


@app.command("book")
def order_book(
    ctx: typer.Context,
    symbol: str = typer.Argument(...),
    limit: int = typer.Option(10, min=1, max=100),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    payload = invoke(lambda: gateway_for(ctx, private=False).order_book(symbol, limit))
    emit(payload, json_output=json_output)
