from __future__ import annotations

import threading
from io import StringIO

from rich.console import Console

from weex_cli.presentation.human import TerminalExecutionProgress, render_human


def rendered(payload) -> tuple[bool, str]:
    stream = StringIO()
    console = Console(file=stream, color_system=None, width=140)
    handled = render_human(payload, console)
    return handled, stream.getvalue()


class FakeClock:
    def __init__(self, now: float = 10.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def progress_text(progress: TerminalExecutionProgress, console: Console) -> str:
    return "".join(segment.text for segment in console.render(progress))


def test_terminal_progress_counts_down_without_sleeping() -> None:
    clock = FakeClock()
    console = Console(file=StringIO(), color_system=None, width=200, force_terminal=True)
    progress = TerminalExecutionProgress(
        console,
        monotonic=clock,
        interactive=True,
        auto_refresh=False,
    )
    try:
        progress({"event": "hold_started", "round": 1, "seconds": 5})
        assert "已等待 0.0秒 / 剩余 5.0秒" in progress_text(progress, console)

        clock.now += 2
        assert "已等待 2.0秒 / 剩余 3.0秒" in progress_text(progress, console)
    finally:
        progress.close()


def test_terminal_progress_keeps_concurrent_leg_waits_visible() -> None:
    clock = FakeClock()
    console = Console(file=StringIO(), color_system=None, width=200, force_terminal=True)
    progress = TerminalExecutionProgress(
        console,
        monotonic=clock,
        interactive=True,
        auto_refresh=False,
    )
    btc_wait = {
        "event": "leg_progress",
        "progress_event": "wait",
        "waiting_for": "maker_fill",
        "round": 1,
        "sequence": 1,
        "symbol": "BTC",
        "action": "open",
        "elapsed_ms": 2_000,
        "remaining_ms": 8_000,
        "filled_quantity": "0.001",
        "order_quantity": "0.002",
    }
    eth_wait = {
        **btc_wait,
        "sequence": 2,
        "symbol": "ETH",
        "filled_quantity": "0.01",
        "order_quantity": "0.02",
    }
    try:
        progress(btc_wait)
        progress(eth_wait)
        output = progress_text(progress, console)
        normalized = " ".join(output.split())

        assert "BTC 开仓" in output
        assert "ETH 开仓" in output
        assert "本单成交 0.001/0.002" in normalized
        assert "本单成交 0.01/0.02" in normalized
    finally:
        progress.close()


def test_terminal_progress_names_submission_recovery_wait() -> None:
    console = Console(file=StringIO(), color_system=None, width=200, force_terminal=True)
    progress = TerminalExecutionProgress(console, interactive=True, auto_refresh=False)
    try:
        progress(
            {
                "event": "leg_progress",
                "progress_event": "wait",
                "waiting_for": "submission_recovery",
                "round": 1,
                "sequence": 1,
                "symbol": "BTC",
                "action": "open",
                "elapsed_ms": 1_000,
                "remaining_ms": 10_000,
            }
        )

        assert "按客户订单号确认下单结果" in progress_text(progress, console)
    finally:
        progress.close()


def test_terminal_progress_names_active_pair_leg_and_hard_deadline() -> None:
    console = Console(file=StringIO(), color_system=None, width=240, force_terminal=True)
    progress = TerminalExecutionProgress(console, interactive=True, auto_refresh=False)
    try:
        progress(
            {
                "event": "pair_wait_progress",
                "round": 1,
                "action": "open",
                "symbols": ("BTC", "ETH"),
                "active_symbols": ("BTC",),
                "completed_symbols": ("ETH",),
                "elapsed_ms": 12_000,
                "remaining_ms": 48_000,
            }
        )

        output = progress_text(progress, console)
        assert "BTC 开仓" in output
        assert "已等待 12.0秒 / 剩余 48.0秒" in output
        assert "到期后自动撤单并核验仓位" in output

        progress({"event": "pair_wait_completed", "round": 1, "action": "open"})
        assert progress_text(progress, console) == ""
    finally:
        progress.close()


def test_terminal_progress_completion_clears_wait_and_stops_live_area() -> None:
    console = Console(file=StringIO(), color_system=None, width=120, force_terminal=True)
    progress = TerminalExecutionProgress(console, interactive=True, auto_refresh=False)
    try:
        progress({"event": "round_gap_started", "round": 1, "seconds": 4})
        assert "等待进入下一周期" in progress_text(progress, console)

        progress({"event": "round_gap_completed", "round": 1, "seconds": 4})
        assert progress_text(progress, console) == ""
        assert progress._live_started is False
    finally:
        progress.close()


def test_terminal_live_io_never_runs_while_holding_the_progress_state_lock() -> None:
    console = Console(file=StringIO(), color_system=None, width=120, force_terminal=True)
    progress = TerminalExecutionProgress(console, interactive=True, auto_refresh=False)
    lock_checks: list[bool] = []

    class LockProbeLive:
        def probe(self) -> None:
            acquired: list[bool] = []

            def contend() -> None:
                locked = progress._lock.acquire(timeout=0.2)
                acquired.append(locked)
                if locked:
                    progress._lock.release()

            thread = threading.Thread(target=contend)
            thread.start()
            thread.join(timeout=1)
            lock_checks.append(acquired == [True])

        def start(self, *, refresh: bool) -> None:
            self.probe()

        def update(self, renderable, *, refresh: bool) -> None:
            self.probe()

        def stop(self) -> None:
            self.probe()

    progress._live = LockProbeLive()  # type: ignore[assignment]
    progress({"event": "round_gap_started", "round": 1, "seconds": 1})
    progress.close()

    assert lock_checks == [True, True]


def test_terminal_progress_noninteractive_mode_uses_static_logs() -> None:
    stream = StringIO()
    console = Console(file=stream, color_system=None, width=120)
    progress = TerminalExecutionProgress(console, interactive=False, auto_refresh=False)

    progress({"event": "hold_started", "round": 3, "seconds": 5})
    progress.close()

    output = stream.getvalue()
    assert "双边持仓等待" in output
    assert "周期 3 / 5s" in output


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
    assert "WEEX 状态" in output
    assert "持仓" in output and "持仓中 (1)" in output
    assert "当前委托" in output

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
    assert "空仓" in unavailable
    assert "当前委托不可用" in unavailable


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
    assert "交易活动" in output
    assert "1000 SUSDT" in output
    assert "统计结果不完整" in output
    assert "近期成交" in output


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
    assert "演练计划 · Maker 压力测试" in output
    assert "10000 SUSDT" in output
    assert "精确确认短语" in output


def test_beta_volume_plan_and_execution_render_as_operator_focused_tables() -> None:
    _, plan_output = rendered(
        {
            "schema_version": 2,
            "kind": "beta_volume_plan",
            "status": "dry_run",
            "plan": {
                "mode": "live",
                "target_turnover_quote": "50",
                "round_turnover_quote": "50",
                "estimated_turnover_quote": "53.18",
                "leverage": 5,
                "margin_mode": "isolated",
                "legs": [
                    {"symbol": "BTC", "quantity": "0.0003", "position_side": "long"},
                    {"symbol": "ETH", "quantity": "0.004", "position_side": "short"},
                ],
            },
            "confirm": "EXECUTE WEEX LIVE BETA-VOLUME ...",
        }
    )
    assert "BTC 多头 / ETH 空头 Beta 交易量" in plan_output
    assert "53.18 USDT" in plan_output
    assert "0.0003 BTC (多头)" in plan_output

    handled, result_output = rendered(
        {
            "schema_version": 2,
            "kind": "beta_volume_execution",
            "status": "completed",
            "reason": "paired_target_completed",
            "target_turnover_quote": "50",
            "executed_quote_volume": "53.18",
            "target_achievement_percent": "106.36",
            "excess_quote": "3.18",
            "elapsed_ms": 75_000,
            "accounting": {
                "source": "user_trades",
                "fill_count": 4,
                "maker_count": 4,
                "taker_count": 0,
                "maker_only": True,
                "commission_by_asset": {"USDT": "0.01"},
                "realized_pnl": "-0.02",
            },
            "legs": [
                {
                    "sequence": 1,
                    "action": "open",
                    "symbol": "BTC",
                    "position_side": "long",
                    "verification_status": "verified",
                    "quote_volume": "19.2",
                    "fill_count": 1,
                    "elapsed_ms": 10_000,
                    "submissions": 1,
                    "cancels": 0,
                }
            ],
            "cycles": [
                {
                    "round": 1,
                    "status": "completed",
                    "executed_quote_volume": "53.18",
                    "actual_open_beta": "0.51",
                    "planned_open_beta": "0.5",
                    "elapsed_ms": 75_000,
                }
            ],
            "final_positions": {"BTC_LONG": 0, "ETH_SHORT": 0},
            "reconciliation_required": False,
        }
    )
    assert handled is True
    assert "Beta 交易量 · 已完成" in result_output
    assert "53.18 / 50 USDT" in result_output
    assert "4 / 0" in result_output
    assert "执行交易腿" in result_output
    assert "配对周期" in result_output
    assert "BTC 多头" in result_output


def test_beta_recovery_renders_authoritative_volume_and_execution_counts() -> None:
    handled, output = rendered(
        {
            "kind": "beta_volume_recovery",
            "status": "completed",
            "reason": "maker_recovery_completed",
            "symbol": "ETH",
            "position_side": "short",
            "executed_quote_volume": "76.44942",
            "final_position": 0,
            "accounting": {
                "fill_count": 3,
                "maker_count": 3,
                "taker_count": 0,
                "unknown_liquidity_count": 0,
                "commission_by_asset": {"USDT": "0.01528987"},
                "realized_pnl": "-0.12259",
            },
            "legs": [{"elapsed_ms": 76093, "submissions": 7, "cancels": 6}],
        }
    )

    assert handled is True
    assert "Beta Maker 恢复 · 已完成" in output
    assert "76.44942 USDT" in output
    assert "3 / 0 / 0" in output
    assert "7 / 6" in output
    assert "1分16.1秒" in output
    assert "最终持仓" in output and "0" in output


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
    assert "3/3 轮" in output
    assert "20分46.5秒" in output
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
    assert "Maker 流程 · 已完成" in output
    assert "4.0秒" in output
    assert "最终持仓" in output and "0" in output
    assert "report.md" in output


def test_message_lists_and_fallback_rendering() -> None:
    handled, message = rendered({"view": "message", "status": "ok", "message": "Already flat."})
    assert handled is True and "已经处于空仓状态" in message
    handled, empty = rendered([])
    assert handled is True and "暂无记录" in empty
    handled, rows = rendered([{"symbol": "BTC", "side": "BUY", "size": "1"}])
    assert handled is True and "交易对" in rows and "BTC" in rows
    handled, scalar_rows = rendered(["one", "two"])
    assert handled is True and "one" in scalar_rows
    assert rendered({"unrecognized": True})[0] is False
    assert rendered("plain text")[0] is False
