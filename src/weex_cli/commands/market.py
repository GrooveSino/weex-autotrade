from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Annotated

import typer

from weex_cli.cli_support import gateway_for, invoke
from weex_cli.market_collector import (
    MarketCollector,
    TickStore,
    WebSocketMarketCollector,
    install_stop_handlers,
    run_market_collector,
    run_websocket_market_collector,
)
from weex_cli.output import emit

app = typer.Typer(help="Read public WEEX contract market data.")
DEFAULT_COLLECTOR_DB_PATH = Path("data/weex.db")


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


@app.command("collect")
def collect(
    ctx: typer.Context,
    db_path: Annotated[Path, typer.Option("--db-path")] = DEFAULT_COLLECTOR_DB_PATH,
    symbols: str = typer.Option("BTC,ETH", "--symbols"),
    transport: str = typer.Option("websocket", "--transport", help="websocket or rest"),
    poll_interval_seconds: float = typer.Option(1.0, "--poll-interval-seconds", min=0.1),
    retention_hours: float = typer.Option(12.0, "--retention-hours", min=0.1),
    cleanup_interval_seconds: float = typer.Option(300.0, "--cleanup-interval-seconds", min=1.0),
    log_interval_seconds: float = typer.Option(60.0, "--log-interval-seconds", min=1.0),
    once: bool = typer.Option(False, "--once", help="Collect one BTC/ETH sample and exit."),
) -> None:
    """Continuously write public WEEX prices to a weex-calc SQLite database."""
    selected_symbols = tuple(item.strip() for item in symbols.split(",") if item.strip())
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    stop_event = threading.Event()
    install_stop_handlers(stop_event)
    selected_transport = transport.strip().lower()
    if selected_transport not in {"websocket", "rest"}:
        raise typer.BadParameter("transport must be websocket or rest")
    with TickStore(db_path, retention_hours=retention_hours) as store:
        if selected_transport == "websocket":
            stream_collector = WebSocketMarketCollector(store, selected_symbols)
            stats = run_websocket_market_collector(
                stream_collector,
                poll_interval_seconds=poll_interval_seconds,
                cleanup_interval_seconds=cleanup_interval_seconds,
                log_interval_seconds=log_interval_seconds,
                once=once,
                stop_event=stop_event,
            )
        else:
            poll_collector = MarketCollector(gateway_for(ctx, private=False), store, selected_symbols)
            stats = run_market_collector(
                poll_collector,
                poll_interval_seconds=poll_interval_seconds,
                cleanup_interval_seconds=cleanup_interval_seconds,
                log_interval_seconds=log_interval_seconds,
                once=once,
                stop_event=stop_event,
            )
    if once:
        emit(
            {
                "status": "ok" if stats.cycles == 1 else "error",
                "cycles": stats.cycles,
                "rows_written": stats.rows_written,
                "rows_deleted": stats.rows_deleted,
                "errors": stats.errors,
                "ignored_ticks": stats.ignored_ticks,
                "prices": stats.last_prices,
                "transport": selected_transport,
                "db_path": str(db_path.expanduser().resolve()),
            },
            json_output=True,
        )
        if stats.cycles != 1:
            raise typer.Exit(1)
