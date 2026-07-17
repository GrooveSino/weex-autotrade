from __future__ import annotations

from io import StringIO

from rich.console import Console

from weex_cli.human_output import render_human


def rendered(payload) -> tuple[bool, str]:
    stream = StringIO()
    console = Console(file=stream, color_system=None, width=140)
    handled = render_human(payload, console)
    return handled, stream.getvalue()


def test_status_renders_overview_positions_orders_and_partial_errors() -> None:
    handled, output = rendered(
        {
            "view": "status",
            "mode": "demo",
            "symbol": "BTC",
            "credentials": {"api": True, "web": True},
            "position": {
                "count": 1,
                "rows": [{"symbol": "BTCSUSDT", "side": "LONG", "size": "0.0159", "unrealizePnl": "1.2"}],
                "error": None,
            },
            "orders": {
                "count": 1,
                "rows": [{"symbol": "BTCSUSDT", "side": "SELL", "status": "OPEN", "price": "63000"}],
                "error": None,
            },
        }
    )

    assert handled is True
    assert "WEEX status" in output
    assert "Position" in output and "Open (1)" in output
    assert "Positions" in output and "Open orders" in output

    _, unavailable = rendered(
        {
            "view": "status",
            "mode": "demo",
            "symbol": "BTC",
            "credentials": {"api": False, "web": None},
            "position": {"count": 0, "rows": [], "error": None},
            "orders": {"count": None, "rows": [], "error": "SubmissionUncertainError"},
        }
    )
    assert "Flat" in unavailable
    assert "Open orders unavailable" in unavailable


def test_activity_renders_summary_warning_and_detail_rows() -> None:
    handled, output = rendered(
        {
            "view": "activity",
            "complete": False,
            "start_datetime": "2026-07-17T00:00:00Z",
            "end_datetime": "2026-07-17T01:00:00Z",
            "summary": {
                "quote_asset": "SUSDT",
                "total_quote_volume": "1000",
                "opening_quote_volume": "500",
                "closing_quote_volume": "500",
                "maker_quote_volume": "1000",
                "taker_quote_volume": "0",
                "trade_count": 2,
            },
            "warnings": ["Totals are incomplete."],
            "trades": [
                {
                    "symbol": "BTCSUSDT",
                    "side": "BUY",
                    "position_action": "open",
                    "quantity": "0.01",
                    "price": "63000",
                    "quote_quantity": "630",
                    "maker": True,
                }
            ],
        }
    )

    assert handled is True
    assert "Trading activity" in output
    assert "1000 SUSDT" in output
    assert "Totals are incomplete" in output
    assert "Recent executions" in output


def test_dry_run_renders_plan_and_exact_confirmation() -> None:
    handled, output = rendered(
        {
            "status": "dry_run",
            "plan": {
                "mode": "demo",
                "symbol": "BTC",
                "target_quote_volume": "10000",
                "fills": 10,
                "rounds": 3,
                "max_position_quote": "1200",
                "timeout_seconds_per_order": 120,
            },
            "confirm": "EXECUTE WEEX DEMO MAKER SOAK BTC ...",
        }
    )

    assert handled is True
    assert "Dry run · Maker soak" in output
    assert "10000 SUSDT" in output
    assert "Exact confirmation" in output


def test_soak_result_renders_round_table_and_report() -> None:
    handled, output = rendered(
        {
            "status": "completed",
            "rounds_requested": 3,
            "rounds_completed": 3,
            "total_quote_volume": "30157.8",
            "elapsed_seconds": 1246.5,
            "rounds": [
                {
                    "round": 1,
                    "status": "completed",
                    "total_quote_volume": "10056",
                    "elapsed_seconds": 406,
                    "submission_count": 29,
                    "observation_error_count": 0,
                    "final_position": 0,
                    "active_order_count": 0,
                }
            ],
            "report_path": "artifacts/reports/soak.md",
        }
    )

    assert handled is True
    assert "3/3 rounds" in output
    assert "20m 46.5s" in output
    assert "10056 SUSDT" in output
    assert "artifacts/reports/soak.md" in output


def test_single_maker_result_renders_success_and_unknown_values() -> None:
    handled, output = rendered(
        {
            "status": "completed",
            "reason": "position_flattened",
            "maker_only": True,
            "final_position": 0,
            "active_order_count": 0,
            "execution": {
                "quote_volume": 1005.9,
                "elapsed_ms": 4004,
                "submissions": 1,
            },
            "report_path": "report.md",
        }
    )

    assert handled is True
    assert "Maker workflow · completed" in output
    assert "4.0s" in output
    assert "Final position" in output and "0" in output
    assert "report.md" in output


def test_message_lists_and_fallback_rendering() -> None:
    handled, message = rendered({"view": "message", "status": "ok", "message": "Already flat."})
    assert handled is True and "Already flat" in message
    handled, empty = rendered([])
    assert handled is True and "No records" in empty
    handled, rows = rendered([{"symbol": "BTC", "side": "BUY", "size": "1"}])
    assert handled is True and "Symbol" in rows and "BTC" in rows
    handled, scalar_rows = rendered(["one", "two"])
    assert handled is True and "one" in scalar_rows
    assert rendered({"unrecognized": True})[0] is False
    assert rendered("plain text")[0] is False
