from __future__ import annotations

import pytest

from weex_cli.config import Settings
from weex_cli.errors import SafetyError
from weex_cli.models import OrderIntent
from weex_cli.redaction import redact, redact_text
from weex_cli.safety import action_confirmation, order_confirmation, require_execution


def _intent(mode: str = "demo") -> OrderIntent:
    return OrderIntent.create(
        mode=mode,
        symbol="BTC",
        side="buy",
        position_side="long",
        order_type="limit",
        quantity="0.001",
        price="60000",
        client_order_id="safety-1",
    )


def test_order_confirmation_is_exact_and_does_not_depend_on_client_id() -> None:
    assert order_confirmation(_intent()) == "EXECUTE WEEX DEMO ORDER BTCSUSDT BUY LONG LIMIT 0.001 60000 POST_ONLY"
    assert action_confirmation("live", "cancel", "BTCUSDT", 123) == "EXECUTE WEEX LIVE CANCEL BTCUSDT 123"


def test_live_execution_requires_environment_gate() -> None:
    expected = order_confirmation(_intent("live"))
    with pytest.raises(SafetyError, match="live trading is disabled"):
        require_execution(
            execute=True,
            supplied=expected,
            expected=expected,
            mode="live",
            settings=Settings.load(environ={}),
        )


@pytest.mark.parametrize("execute,supplied", [(False, "x"), (True, "wrong")])
def test_execution_flag_and_phrase_are_required(execute: bool, supplied: str) -> None:
    expected = order_confirmation(_intent())
    with pytest.raises(SafetyError):
        require_execution(
            execute=execute,
            supplied=supplied,
            expected=expected,
            mode="demo",
            settings=Settings.load(environ={}),
        )


def test_redaction_removes_secrets_from_nested_payloads_and_text() -> None:
    assert redact(
        {
            "apiKey": "abc",
            "WEEX_API_KEY": "def",
            "nested": {"access_passphrase": "xyz", "databasePassword": "pw"},
            "U-TOKEN": "web-token",
            "terminalCode": "fingerprint",
        }
    ) == {
        "apiKey": "[REDACTED]",
        "WEEX_API_KEY": "[REDACTED]",
        "nested": {"access_passphrase": "[REDACTED]", "databasePassword": "[REDACTED]"},
        "U-TOKEN": "[REDACTED]",
        "terminalCode": "[REDACTED]",
    }
    assert "secret=[REDACTED]" in redact_text("request secret=hello failed")
    assert "visible-secret" not in redact_text("WEEX_API_SECRET=visible-secret")
    assert "web-token" not in redact_text("cc_token=web-token")
