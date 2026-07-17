from __future__ import annotations

import typer

from weex_cli.cli_support import gateway_for, invoke, selected_mode
from weex_cli.output import emit
from weex_cli.trade_reporting import TradeReportService, current_timestamp_ms, parse_timestamp

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
