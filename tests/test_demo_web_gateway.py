from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from weex_cli.core.config import Settings
from weex_cli.core.errors import ValidationError
from weex_cli.exchange.rest.demo_web import (
    CANCEL_ALL_PATH,
    CANCEL_ORDER_PATH,
    DEMO_COIN_ID,
    OPEN_ORDERS_PATH,
    ORDER_HISTORY_PATH,
    WEB_BASE_URL,
    DemoWebGateway,
    _random_text,
    web_signature,
)


class FakeTransport:
    def __init__(self, responses: list[Mapping[str, Any]]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, Mapping[str, str], Mapping[str, Any], float]] = []

    def __call__(self, url, headers, payload, timeout):
        self.calls.append((url, headers, payload, timeout))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def settings() -> Settings:
    return Settings.load(
        environ={
            "WEEX_WEB_CC_TOKEN": "cc-token",
            "WEEX_WEB_TERMINAL_CODE": "terminal-code",
            "WEEX_TIMEOUT_MS": "20000",
        }
    )


def gateway(transport: FakeTransport) -> DemoWebGateway:
    return DemoWebGateway(
        settings(),
        transport,
        now_ms=lambda: 1784250000123,
        random_text=lambda length: "v" * length,
    )


def open_response(*rows: dict[str, Any]) -> dict[str, Any]:
    return {"code": "00000", "data": {"dataList": list(rows)}}


def history_response(*rows: dict[str, Any], next_flag=False, next_key=None) -> dict[str, Any]:
    return {
        "code": "SUCCESS",
        "data": {"dataList": list(rows), "nextFlag": next_flag, "nextKey": next_key},
    }


def test_open_orders_uses_demo_coin_and_frontend_signature_headers() -> None:
    transport = FakeTransport([open_response({"id": "1", "contractName": "BTC/SUSDT"})])
    rows = gateway(transport).open_orders("BTCSUSDT")

    assert rows == [{"id": "1", "contractName": "BTC/SUSDT"}]
    url, headers, payload, timeout = transport.calls[0]
    assert url == f"{WEB_BASE_URL}{OPEN_ORDERS_PATH}"
    assert payload["filterCoinIdList"] == [DEMO_COIN_ID]
    assert payload["languageType"] == 1
    assert payload["filterOrderStatusList"] == ["OPEN", "PENDING", "CANCELING"]
    assert headers["U-TOKEN"] == "cc-token"
    assert headers["X-SIG"] == web_signature(1784250000123, "v" * 32, "terminal-code")
    assert timeout == 20


def test_vs_matches_frontend_fixed_character_pattern() -> None:
    value = _random_text(32)
    assert len(value) == 32
    assert [value[index] for index in (1, 3, 7, 14, 20, 29)] == ["5", "7", "8", "9", "7", "6"]


def test_cancel_order_verifies_that_order_is_absent() -> None:
    transport = FakeTransport([{"code": "00000"}, open_response()])
    result = gateway(transport).cancel_order("123")

    assert result["status"] == "verified_canceled"
    assert transport.calls[0][0] == f"{WEB_BASE_URL}{CANCEL_ORDER_PATH}"
    assert transport.calls[0][2]["orderIdList"] == ["123"]
    assert transport.calls[1][0] == f"{WEB_BASE_URL}{OPEN_ORDERS_PATH}"


def test_order_history_paginates_and_preserves_cancel_reason() -> None:
    transport = FakeTransport(
        [
            history_response({"id": "2", "cancelReason": "COULD_NOT_FILL"}, next_flag=True, next_key="next"),
            history_response({"id": "1", "cancelReason": "USER_CANCELED"}),
        ]
    )

    rows = gateway(transport).order_history(limit=20)

    assert [row["id"] for row in rows] == ["2", "1"]
    assert rows[0]["cancelReason"] == "COULD_NOT_FILL"
    assert transport.calls[0][0] == f"{WEB_BASE_URL}{ORDER_HISTORY_PATH}"
    assert transport.calls[1][2]["nextKey"] == "next"


def test_cancel_order_reports_pending_when_order_remains_open() -> None:
    transport = FakeTransport([{"code": "00000"}, open_response({"id": "123"})])
    result = gateway(transport).cancel_order("123")
    assert result["status"] == "cancel_pending"
    assert result["remaining_order_ids"] == ["123"]


def test_cancel_verification_failure_is_uncertain_without_resubmission() -> None:
    transport = FakeTransport([{"code": "00000"}, ValidationError("query failed")])
    result = gateway(transport).cancel_order("123")
    assert result["status"] == "uncertain"
    assert len([call for call in transport.calls if call[0].endswith(CANCEL_ORDER_PATH)]) == 1


def test_cancel_all_uses_frontend_demo_filter_and_verifies_ids() -> None:
    transport = FakeTransport([open_response({"id": "1"}, {"orderId": "2"}), {"code": "00000"}, open_response()])
    result = gateway(transport).cancel_all_orders()

    assert result["status"] == "verified_canceled"
    url, _, payload, _ = transport.calls[1]
    assert url == f"{WEB_BASE_URL}{CANCEL_ALL_PATH}"
    assert payload["filterCoinIdList"] == [DEMO_COIN_ID]
    assert payload["filterOrderStatusList"] == ["OPEN", "PENDING"]


def test_symbol_scoped_cancel_all_is_rejected_instead_of_canceling_everything() -> None:
    transport = FakeTransport([])
    with pytest.raises(ValidationError, match="cannot safely map"):
        gateway(transport).cancel_all_orders("BTC")
    assert transport.calls == []


def test_exchange_error_is_not_treated_as_success() -> None:
    transport = FakeTransport([{"code": "401", "msg": "not logged in"}])
    with pytest.raises(ValidationError, match="code 401"):
        gateway(transport).open_orders()
