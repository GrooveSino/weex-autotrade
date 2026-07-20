from __future__ import annotations

import json
from decimal import ROUND_DOWN, Decimal
from pathlib import Path

from typer.testing import CliRunner

from weex_cli.cli import app
from weex_cli.commands import live
from weex_cli.config import Settings

runner = CliRunner()


class PlanGateway:
    def order_book(self, symbol: str, limit: int) -> dict[str, object]:
        return {"bids": [[99, 10]], "asks": [[101, 10]]}

    def amount_step(self, symbol: str) -> Decimal:
        return Decimal("0.1")

    def amount_to_precision(self, symbol: str, value: Decimal) -> Decimal:
        return value.quantize(Decimal("0.1"), rounding=ROUND_DOWN)


def test_live_maker_volume_cli_plans_by_default_with_exact_gate(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(live, "gateway_for", lambda ctx: PlanGateway())
    result = runner.invoke(
        app,
        [
            "live",
            "maker-volume",
            "--target",
            "5000",
            "--round",
            "500",
            "--leverage",
            "2",
            "--plan-directory",
            str(tmp_path),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "dry_run"
    assert payload["plan"]["estimated_rounds"] == 10
    assert payload["confirm"].startswith(
        "EXECUTE WEEX LIVE MAKER VOLUME BTC TARGET_5000 ROUND_500 LEVERAGE_2 "
        "TIMEOUT_120 RECOVERY_3 EMPTY_3 POST_ONLY LMV-"
    )


def test_live_maker_volume_cli_execution_still_requires_environment_gate(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(live, "gateway_for", lambda ctx: PlanGateway())
    disabled = Settings.load(
        environ={
            "WEEX_API_KEY": "key",
            "WEEX_API_SECRET": "secret",
            "WEEX_API_PASSPHRASE": "pass",
            "WEEX_LIVE_TRADING_ENABLED": "false",
        }
    )
    monkeypatch.setattr(live, "settings_for", lambda ctx: disabled)
    planned = runner.invoke(
        app,
        ["live", "maker-volume", "--plan-directory", str(tmp_path), "--json"],
    )
    assert planned.exit_code == 0, planned.output
    payload = json.loads(planned.output)

    executed = runner.invoke(
        app,
        [
            "live",
            "maker-volume",
            "--plan-id",
            payload["plan"]["plan_id"],
            "--plan-directory",
            str(tmp_path),
            "--execute",
            "--confirm",
            payload["confirm"],
        ],
    )

    assert executed.exit_code == 1
    assert "实盘交易未启用" in executed.output
