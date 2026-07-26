from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from typer._click.utils import strip_ansi
from typer.testing import CliRunner

from weex_cli.cli import app
from weex_cli.commands import home
from weex_cli.commands.read import account, market, orders, trades
from weex_cli.commands.trade import order, risk
from weex_cli.commands.workflows import maker_cli
from weex_cli.core.config import Settings
from weex_cli.presentation.i18n import set_language

runner = CliRunner()


def invoke_json(args: list[str], env: dict[str, str] | None = None) -> dict:
    result = runner.invoke(app, [*args, "--json"], env=env or {})
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def test_version_and_help() -> None:
    assert runner.invoke(app, ["--version"]).output.strip() == "0.1.0"
    output = runner.invoke(app, ["--help"]).output
    assert "日常操作" in output
    assert all(command in output for command in ("status", "maker", "activity", "advanced"))


def test_cli_can_switch_the_complete_help_surface_to_english() -> None:
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [str(root / "weex"), "--lang", "en", "--help"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "Usage:" in result.stdout
    assert "Daily workflow" in result.stdout
    assert "Show this message and exit" in result.stdout
    assert "日常操作" not in result.stdout


def test_language_switch_does_not_change_json_protocol() -> None:
    try:
        chinese = runner.invoke(app, ["maker", "run", "--json"])
        english = runner.invoke(app, ["--lang", "en", "maker", "run", "--json"])
        human_english = runner.invoke(app, ["--lang", "en", "maker", "run"])
    finally:
        set_language("zh")

    assert chinese.exit_code == english.exit_code == human_english.exit_code == 0
    assert json.loads(chinese.output) == json.loads(english.output)
    assert "Dry run · Maker run" in human_english.output
    assert "Exact confirmation" in human_english.output


def test_human_maker_help_uses_practical_defaults() -> None:
    output = runner.invoke(app, ["maker", "run", "--help"]).output

    assert "默认值：BTC" in output
    assert "默认值：10000" in output
    assert "默认值：10" in output
    assert "默认值：1200" in output
    assert "默认值：120" in output


def test_live_beta_volume_help_exposes_parallel_cycle_defaults() -> None:
    output = strip_ansi(runner.invoke(app, ["live", "beta-volume", "--help"]).output)

    assert "BTC 多头/ETH 空头并发" in output
    assert "默认值：5000" in output
    assert "默认值：500" in output
    assert "--leverage" in output
    assert "默认值：auto" in output
    assert "--recovery-attempts" not in output
    assert "--max-empty-rounds" not in output
    assert "--cooldown" not in output
    assert "--allow-low-confidence-beta" not in output


def test_human_maker_dry_runs_have_exact_confirmations_and_json_compatibility() -> None:
    run = invoke_json(["maker", "run"])
    soak = invoke_json(["maker", "soak"])

    assert run["confirm"] == "EXECUTE WEEX DEMO MAKER VOLUME BTC TARGET_10000 FILLS_10 MAX_POSITION_1200 TIMEOUT_120"
    assert soak["confirm"] == (
        "EXECUTE WEEX DEMO MAKER SOAK BTC TARGET_10000 FILLS_10 ROUNDS_3 MAX_POSITION_1200 TIMEOUT_120"
    )
    human = runner.invoke(app, ["maker", "run"])
    assert human.exit_code == 0
    assert "演练计划 · Maker 交易量任务" in human.output
    assert "精确确认短语" in human.output


def test_human_flatten_detects_position_and_handles_already_flat(monkeypatch) -> None:
    fake = CliGateway()
    monkeypatch.setattr(fake, "positions", lambda mode, symbol=None: [{"side": "LONG", "size": "0.0159"}])
    monkeypatch.setattr(maker_cli, "gateway_for", lambda ctx: fake)

    payload = invoke_json(["maker", "flatten"])

    assert payload["quantity"] == "0.0159"
    assert payload["confirm"] == "EXECUTE WEEX DEMO MAKER FLATTEN BTC QUANTITY_0.0159 MAX_POSITION_1200 TIMEOUT_120"

    monkeypatch.setattr(fake, "positions", lambda mode, symbol=None: [])
    flat = invoke_json(["maker", "flatten"])
    assert flat["status"] == "ok"
    assert "already flat" in flat["message"]


def test_status_and_activity_are_one_step_read_only_views(monkeypatch) -> None:
    fake = CliGateway()
    monkeypatch.setattr(home, "gateway_for", lambda ctx: fake)
    monkeypatch.setattr(home, "current_timestamp_ms", lambda: 1784217601000)

    status = invoke_json(["status"])
    activity = invoke_json(["activity", "--hours", "1"])

    assert status["mode"] == "demo"
    assert status["position"]["count"] == 0
    assert status["orders"]["count"] == 1
    assert activity["summary"]["total_quote_volume"] == "10"
    assert activity["trades"] == []


def test_legacy_and_advanced_paths_remain_available(monkeypatch) -> None:
    fake = CliGateway()
    monkeypatch.setattr(market, "gateway_for", lambda ctx, private=False: fake)

    legacy = invoke_json(["market", "ticker", "BTC"])
    advanced = invoke_json(["advanced", "market", "ticker", "BTC"])

    assert legacy == advanced == {"symbol": "BTC", "last": 100}


def test_config_show_never_prints_credentials() -> None:
    payload = invoke_json(
        ["config", "show"],
        env={
            "WEEX_API_KEY": "visible-key",
            "WEEX_API_SECRET": "visible-secret",
            "WEEX_API_PASSPHRASE": "visible-pass",
            "WEEX_WEB_CC_TOKEN": "visible-token",
            "WEEX_WEB_TERMINAL_CODE": "visible-terminal",
        },
    )
    assert payload["credentials_configured"] is True
    assert payload["web_credentials_configured"] is True
    assert "visible" not in json.dumps(payload)


def test_order_plan_and_place_default_to_demo_dry_run() -> None:
    args = ["BTC", "--side", "buy", "--position-side", "long", "--quantity", "0.001", "--price", "60000"]
    plan = invoke_json(["order", "plan", *args])
    place = invoke_json(["order", "place", *args])
    assert plan["intent"]["symbol"] == "BTCSUSDT"
    assert plan["intent"]["time_in_force"] == "POST_ONLY"
    assert place["status"] == "dry_run"
    assert place["confirm"] == "EXECUTE WEEX DEMO ORDER BTCSUSDT BUY LONG LIMIT 0.001 60000 POST_ONLY"


def test_maker_volume_defaults_to_demo_dry_run_with_exact_batch_confirmation() -> None:
    payload = invoke_json(
        [
            "volume",
            "maker",
            "BTC",
            "--target",
            "100000",
            "--fills",
            "10",
            "--max-position",
            "12000",
            "--timeout",
            "120",
        ]
    )
    assert payload["status"] == "dry_run"
    assert payload["confirm"] == (
        "EXECUTE WEEX DEMO MAKER VOLUME BTC TARGET_100000 FILLS_10 MAX_POSITION_12000 TIMEOUT_120"
    )
    assert payload["safety"]["post_only"] is True


def test_maker_benchmark_is_offline_and_passes_without_credentials() -> None:
    payload = invoke_json(
        [
            "volume",
            "benchmark",
            "--train-trials",
            "5",
            "--validation-trials",
            "5",
        ]
    )
    assert payload["status"] == "passed"
    assert payload["simulation_only"] is True
    assert all(payload["acceptance"].values())


@pytest.mark.parametrize(
    "args, action",
    [
        (["account", "configure", "BTC", "--leverage", "10"], "configure_position"),
        (["account", "close", "BTC", "--position-side", "long"], "close_position"),
        (["account", "close-all"], "close_all_positions"),
        (["orders", "cancel", "BTC", "123"], "cancel_order"),
        (["orders", "cancel-all", "--symbol", "BTC"], "cancel_all_orders"),
        (
            ["risk", "tp-sl", "BTC", "--plan-type", "STOP_LOSS", "--trigger-price", "59000", "--position-side", "LONG"],
            "place_tp_sl",
        ),
        (
            ["risk", "bracket", "BTC", "--position-side", "LONG", "--take-profit", "63000", "--stop-loss", "58500"],
            "place_bracket",
        ),
        (
            [
                "risk",
                "replace-stop",
                "BTC",
                "--old-order-id",
                "123",
                "--trigger-price",
                "59000",
                "--position-side",
                "LONG",
            ],
            "replace_stop",
        ),
        (["risk", "modify", "123", "--trigger-price", "59000"], "modify_tp_sl"),
        (["risk", "cancel", "123"], "cancel_algo"),
    ],
)
def test_all_mutations_are_dry_run_by_default(args: list[str], action: str) -> None:
    payload = invoke_json(args)
    assert payload["status"] == "dry_run"
    assert payload["action"] == action
    assert payload["confirm"].startswith("EXECUTE WEEX ")


def test_live_execute_is_blocked_without_live_env_gate() -> None:
    args = [
        "order",
        "place",
        "BTC",
        "--mode",
        "live",
        "--side",
        "buy",
        "--position-side",
        "long",
        "--quantity",
        "0.001",
        "--price",
        "60000",
        "--execute",
        "--confirm",
        "EXECUTE WEEX LIVE ORDER BTCUSDT BUY LONG LIMIT 0.001 60000 POST_ONLY",
    ]
    result = runner.invoke(app, args, env={"WEEX_LIVE_TRADING_ENABLED": "false"})
    assert result.exit_code == 1
    assert "实盘交易未启用" in result.output


def test_bad_order_argument_is_reported_without_traceback() -> None:
    result = runner.invoke(
        app,
        [
            "order",
            "plan",
            "BTC",
            "--side",
            "hold",
            "--position-side",
            "long",
            "--quantity",
            "1",
            "--order-type",
            "market",
        ],
    )
    assert result.exit_code == 1
    assert "side 必须是 buy 或 sell" in result.output
    assert "Traceback" not in result.output


def test_close_rejects_invalid_position_side() -> None:
    result = runner.invoke(app, ["account", "close", "BTC", "--position-side", "flat"])
    assert result.exit_code == 2
    assert "position-side 必须是 LONG 或 SHORT" in result.output


@pytest.mark.parametrize(
    "args, message",
    [
        (
            [
                "risk",
                "tp-sl",
                "BTC",
                "--plan-type",
                "STOP_LOSS",
                "--trigger-price",
                "59000",
                "--position-side",
                "LONG",
                "--quantity",
                "-1",
            ],
            "quantity 必须大于等于 0",
        ),
        (
            [
                "risk",
                "bracket",
                "BTC",
                "--position-side",
                "LONG",
                "--take-profit",
                "58000",
                "--stop-loss",
                "59000",
            ],
            "long take-profit 必须大于止损价",
        ),
        (
            [
                "risk",
                "bracket",
                "BTC",
                "--position-side",
                "SHORT",
                "--take-profit",
                "61000",
                "--stop-loss",
                "60000",
            ],
            "short take-profit 必须小于止损价",
        ),
        (
            ["risk", "modify", "123", "--trigger-price", "59000", "--execute-price", "NaN"],
            "execute_price 必须大于等于 0",
        ),
    ],
)
def test_invalid_risk_arguments_are_rejected(args: list[str], message: str) -> None:
    result = runner.invoke(app, args)
    assert result.exit_code == 1
    assert message in result.output
    assert "Traceback" not in result.output


class CliGateway:
    def __init__(self) -> None:
        self.calls = []
        self.last_client_id = None
        self.algo_ids = []

    def ticker(self, symbol):
        return {"symbol": symbol, "last": 100}

    def order_book(self, symbol, limit):
        return {"symbol": symbol, "limit": limit, "bids": [], "asks": []}

    def balance(self, mode):
        return [{"asset": "SUSDT", "mode": mode}]

    def positions(self, mode, symbol=None):
        return []

    def configure_position(self, symbol, leverage, margin_mode):
        self.calls.append(("configure", symbol, leverage, margin_mode))
        return {"success": True}

    def close_position(self, symbol, position_side):
        self.calls.append(("close", symbol, position_side))
        return {"success": True}

    def close_all_positions(self):
        self.calls.append(("close_all",))
        return []

    def open_orders(self, symbol=None, trigger=False, mode="live"):
        return [{"orderId": "1", "symbol": symbol, "trigger": trigger, "mode": mode}]

    def order_history(self, mode, symbol=None, limit=100, start_time=None, end_time=None):
        if self.last_client_id:
            return [{"clientOrderId": self.last_client_id, "status": "NEW"}]
        return [{"orderId": "2", "symbol": symbol, "status": "FILLED"}]

    def trade_rows(self, mode, symbol, **kwargs):
        return [
            {
                "orderId": "trade-1",
                "symbol": "BTCSUSDT",
                "side": "BUY",
                "positionSide": "LONG",
                "status": "FILLED",
                "executedQty": "0.1",
                "avgPrice": "100",
                "cumQuote": "10",
                "timeInForce": "POST_ONLY",
                "updateTime": 1784217600000,
            }
        ]

    def cancel_order(self, symbol, order_id, trigger=False, mode="live"):
        self.calls.append(("cancel", symbol, order_id, trigger, mode))
        return {"success": True}

    def cancel_all_orders(self, symbol=None, trigger=False, mode="live"):
        self.calls.append(("cancel_all", symbol, trigger, mode))
        return []

    def place_order(self, intent):
        self.last_client_id = intent.client_order_id
        return {"success": True, "clientOrderId": self.last_client_id}

    def place_tp_sl(self, **kwargs):
        self.algo_ids.append(kwargs["client_algo_id"])
        return {"success": True, "orderId": str(len(self.algo_ids))}

    def algo_orders(self, symbol=None, history=False):
        return [{"clientAlgoId": value, "orderId": str(index)} for index, value in enumerate(self.algo_ids, 1)]

    def modify_tp_sl(self, **kwargs):
        self.calls.append(("modify", kwargs))
        return {"success": True}

    def cancel_algo_order(self, order_id):
        self.calls.append(("cancel_algo", order_id))
        return {"success": True}


def live_settings() -> Settings:
    return Settings.load(
        environ={
            "WEEX_API_KEY": "key",
            "WEEX_API_SECRET": "secret",
            "WEEX_API_PASSPHRASE": "pass",
            "WEEX_LIVE_TRADING_ENABLED": "true",
        }
    )


def test_read_commands_with_fake_gateway(monkeypatch) -> None:
    fake = CliGateway()
    monkeypatch.setattr(market, "gateway_for", lambda ctx, private=False: fake)
    monkeypatch.setattr(account, "gateway_for", lambda ctx: fake)
    monkeypatch.setattr(orders, "gateway_for", lambda ctx: fake)
    monkeypatch.setattr(risk, "gateway_for", lambda ctx: fake)
    monkeypatch.setattr(trades, "gateway_for", lambda ctx: fake)
    for args in (
        ["market", "ticker", "BTC"],
        ["market", "book", "BTC"],
        ["account", "balance"],
        ["account", "positions", "--symbol", "BTC"],
        ["orders", "open", "--symbol", "BTC"],
        ["orders", "history", "--symbol", "BTC"],
        ["risk", "orders", "--symbol", "BTC"],
        [
            "trades",
            "report",
            "--mode",
            "demo",
            "--symbol",
            "BTC",
            "--start",
            "2026-07-17T00:00:00+08:00",
            "--end",
            "2026-07-17T23:59:59+08:00",
        ],
    ):
        assert runner.invoke(app, [*args, "--json"]).exit_code == 0


def _execute_from_plan(args: list[str]) -> list[str]:
    plan = invoke_json(args)
    return [*args, "--execute", "--confirm", plan["confirm"], "--json"]


def test_live_account_and_cancel_execution_paths(monkeypatch) -> None:
    fake = CliGateway()
    settings = live_settings()
    monkeypatch.setattr(account, "gateway_for", lambda ctx: fake)
    monkeypatch.setattr(account, "settings_for", lambda ctx: settings)
    monkeypatch.setattr(orders, "gateway_for", lambda ctx: fake)
    monkeypatch.setattr(orders, "settings_for", lambda ctx: settings)
    commands = (
        ["account", "configure", "BTC", "--leverage", "10"],
        ["account", "close", "BTC", "--position-side", "long"],
        ["account", "close-all"],
        ["orders", "cancel", "BTC", "123"],
        ["orders", "cancel-all", "--symbol", "BTC"],
    )
    for args in commands:
        result = runner.invoke(app, _execute_from_plan(args))
        assert result.exit_code == 0, result.output
    assert {call[0] for call in fake.calls} >= {"configure", "close", "close_all", "cancel", "cancel_all"}


def test_demo_cancel_uses_demo_confirmation_and_explicit_mode(monkeypatch) -> None:
    fake = CliGateway()
    settings = Settings.load(environ={})
    monkeypatch.setattr(orders, "gateway_for", lambda ctx: fake)
    monkeypatch.setattr(orders, "settings_for", lambda ctx: settings)
    args = ["orders", "cancel", "BTC", "123", "--mode", "demo"]
    plan = invoke_json(args)
    assert plan["confirm"] == "EXECUTE WEEX DEMO CANCEL BTCSUSDT 123 REGULAR"
    result = runner.invoke(app, _execute_from_plan(args))
    assert result.exit_code == 0, result.output
    assert fake.calls[-1] == ("cancel", "BTC", "123", False, "demo")


def test_order_and_risk_execution_paths(monkeypatch) -> None:
    fake = CliGateway()
    settings = live_settings()
    monkeypatch.setattr(order, "gateway_for", lambda ctx: fake)
    monkeypatch.setattr(order, "settings_for", lambda ctx: settings)
    monkeypatch.setattr(risk, "gateway_for", lambda ctx: fake)
    monkeypatch.setattr(risk, "settings_for", lambda ctx: settings)
    monkeypatch.setattr(fake, "open_orders", lambda symbol=None, trigger=False: [])

    place_args = [
        "order",
        "place",
        "BTC",
        "--mode",
        "live",
        "--side",
        "buy",
        "--position-side",
        "long",
        "--quantity",
        "0.001",
        "--price",
        "60000",
    ]
    assert runner.invoke(app, _execute_from_plan(place_args)).exit_code == 0

    risk_commands = (
        ["risk", "tp-sl", "BTC", "--plan-type", "STOP_LOSS", "--trigger-price", "59000", "--position-side", "LONG"],
        ["risk", "bracket", "BTC", "--position-side", "LONG", "--take-profit", "63000", "--stop-loss", "58500"],
        ["risk", "replace-stop", "BTC", "--old-order-id", "old", "--trigger-price", "59000", "--position-side", "LONG"],
        ["risk", "modify", "123", "--trigger-price", "59000"],
        ["risk", "cancel", "123"],
    )
    for args in risk_commands:
        result = runner.invoke(app, _execute_from_plan(args))
        assert result.exit_code == 0, result.output


def test_doctor_without_network_is_offline_safe() -> None:
    payload = invoke_json(["doctor", "--no-network"])
    assert payload["version"] == "0.1.0"
    assert "dns" in payload
