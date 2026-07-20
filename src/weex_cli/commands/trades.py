from __future__ import annotations

from pathlib import Path

import typer

from weex_cli.cli_support import gateway_for, invoke, selected_mode, settings_for
from weex_cli.output import emit
from weex_cli.trade_reporting import TradeReportService, current_timestamp_ms, parse_timestamp
from weex_cli.trade_volume_cache import DemoTradeVolumeSyncService, SQLiteTradeVolumeLedger, account_fingerprint

app = typer.Typer(help="Report authenticated WEEX fills and executed quote volume.")


@app.command("report")
def report(
    ctx: typer.Context,
    start: str = typer.Option(..., help="Timezone-aware ISO-8601 or Unix seconds/milliseconds"),
    end: str | None = typer.Option(None, help="Defaults to now"),
    symbol: str | None = typer.Option(None),
    mode: str | None = typer.Option(None, help="demo or live"),
    summary_only: bool = typer.Option(False, help="Omit normalized trade rows"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    selected = selected_mode(ctx, mode)
    start_time, end_time = invoke(
        lambda: (
            parse_timestamp(start, name="start"),
            parse_timestamp(end, name="end") if end else current_timestamp_ms(),
        )
    )
    payload = invoke(
        lambda: TradeReportService(gateway_for(ctx)).report(
            mode=selected,
            symbol=symbol,
            start_time=start_time,
            end_time=end_time,
        )
    )
    if summary_only:
        payload = {key: value for key, value in payload.items() if key != "trades"}
    emit(payload, json_output=json_output)


@app.command("sync-volume")
def sync_volume(
    ctx: typer.Context,
    start: str | None = typer.Option(None, help="History start; defaults to 365 days ago"),
    symbol: str | None = typer.Option(None),
    mode: str | None = typer.Option(None, help="Currently demo only"),
    database: str = typer.Option("data/trade-volume.sqlite3", "--database", "--db"),
    max_requests: int = typer.Option(50, min=1, max=500, help="Request budget for this invocation"),
    overlap_seconds: int = typer.Option(60, min=1, max=3600, help="Incremental overlap for late updates"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Backfill once, then return cached account volume using one incremental request."""
    selected = selected_mode(ctx, mode)
    if selected != "demo":
        raise typer.BadParameter("sync-volume currently supports Demo only")
    end_time = current_timestamp_ms()
    start_time = invoke(lambda: parse_timestamp(start, name="start")) if start else end_time - 365 * 86_400_000
    settings = settings_for(ctx)
    credentials = settings.require_credentials()

    def action() -> dict[str, object]:
        with SQLiteTradeVolumeLedger(Path(database)) as ledger:
            return DemoTradeVolumeSyncService(
                gateway_for(ctx),
                ledger,
                account_fingerprint(credentials.api_key),
            ).sync(
                start_time=start_time,
                end_time=end_time,
                symbol=symbol,
                max_requests=max_requests,
                overlap_ms=overlap_seconds * 1000,
            )

    emit(invoke(action), json_output=json_output)
