from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from weex_cli.cli import app
from weex_cli.commands import account, market, order, orders, risk, trades
from weex_cli.config import Settings

runner = CliRunner()


def invoke_json(args: list[str], env: dict[str, str] | None = None) -> dict:
    result = runner.invoke(app, [*args, "--json"], env=env or {})
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def test_version_and_help() -> None:
    assert runner.invoke(app, ["--version"]).output.strip() == "0.1.0"
    assert "Safety-first WEEX contract CLI" in runner.invoke(app, ["--help"]).output


def test_config_show_never_prints_credentials() -> None:
    payload = invoke_json(
        ["config", "show"],
        env={"WEEX_API_KEY": "visible-key", "WEEX_API_SECRET": "visible-secret", "WEEX_API_PASSPHRASE": "visible-pass"},
    )
    assert payload["credentials_configured"] is True
    assert "visible" not in json.dumps(payload)


def test_order_plan_and_place_default_to_demo_dry_run() -> None:
    args = ["BTC", "--side", "buy", "--position-side", "long", "--quantity", "0.001", "--price", "60000"]
    plan = invoke_json(["order", "plan", *args])
    place = invoke_json(["order", "place", *args])
    assert plan["intent"]["symbol"] == "BTCSUSDT"
    assert plan["intent"]["time_in_force"] == "POST_ONLY"
    assert place["status"] == "dry_run"
    assert place["confirm"] == "EXECUTE WEEX DEMO ORDER BTCSUSDT BUY LONG LIMIT 0.001 60000 POST_ONLY"


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
    assert "live trading is disabled" in result.output


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
    assert "side must be buy or sell" in result.output
    assert "Traceback" not in result.output


def test_close_rejects_invalid_position_side() -> None:
    result = runner.invoke(app, ["account", "close", "BTC", "--position-side", "flat"])
    assert result.exit_code == 2
    assert "position-side must be LONG or SHORT" in result.output


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
            "quantity must be zero or greater",
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
            "long take-profit must be greater than stop-loss",
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
            "short take-profit must be less than stop-loss",
        ),
        (
            ["risk", "modify", "123", "--trigger-price", "59000", "--execute-price", "NaN"],
            "execute_price must be zero or greater",
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

    def open_orders(self, symbol=None, trigger=False):
        return [{"orderId": "1", "symbol": symbol, "trigger": trigger}]

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

    def cancel_order(self, symbol, order_id, trigger=False):
        self.calls.append(("cancel", symbol, order_id, trigger))
        return {"success": True}

    def cancel_all_orders(self, symbol=None, trigger=False):
        self.calls.append(("cancel_all", symbol, trigger))
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
