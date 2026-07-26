from __future__ import annotations

import hashlib
import json
import secrets
import string
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Mapping
from typing import Any

from weex_cli.core.config import Settings, WebCredentials
from weex_cli.core.errors import SubmissionUncertainError, ValidationError

WEB_BASE_URL = "https://http-gateway2.weex.com"
OPEN_ORDERS_PATH = "/api/v1/private/order/getActiveOrderPage2"
ORDER_HISTORY_PATH = "/api/v1/private/order/v2/getHistoryOrderPage"
CANCEL_ORDER_PATH = "/api/v1/private/order/cancelOrderById"
CANCEL_ALL_PATH = "/api/v1/private/order/cancelAllOrder"
APP_VERSION = "2.0.2"
TERMINAL_TYPE = "1"
DEMO_COIN_ID = 64
LANGUAGE_TYPE_ZH_CN = 1
SUCCESS_CODES = {"0", "00000", "200", "200000", "SUCCESS"}

Transport = Callable[[str, Mapping[str, str], Mapping[str, Any], float], Mapping[str, Any]]


class DemoWebGateway:
    """Unsupported WEEX Web API boundary, hard-gated to the Demo account surface."""

    def __init__(
        self,
        settings: Settings,
        transport: Transport | None = None,
        *,
        now_ms: Callable[[], int] | None = None,
        random_text: Callable[[int], str] | None = None,
    ) -> None:
        self.settings = settings
        self._transport = transport or _urllib_post
        self._now_ms = now_ms or (lambda: time.time_ns() // 1_000_000)
        self._random_text = random_text or _random_text

    def open_orders(self, symbol: str | None = None, *, page_size: int = 100) -> list[dict[str, Any]]:
        if not 1 <= page_size <= 100:
            raise ValidationError("Demo Web open-order page_size must be between 1 and 100")
        response = self._post(
            OPEN_ORDERS_PATH,
            {
                "filterCoinIdList": [DEMO_COIN_ID],
                "pageNo": 0,
                "pageSize": page_size,
                "languageType": LANGUAGE_TYPE_ZH_CN,
                "sign": "SIGN",
                "timeZone": "",
                "filterOrderStatusList": ["OPEN", "PENDING", "CANCELING"],
            },
        )
        rows, _ = _data_page(response, "Demo Web open-order query")
        if symbol is None:
            return rows
        return [row for row in rows if _matches_symbol(row, symbol)]

    def order_history(self, symbol: str | None = None, *, limit: int = 100) -> list[dict[str, Any]]:
        if not 1 <= limit <= 1000:
            raise ValidationError("Demo Web order-history limit must be between 1 and 1000")
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        next_key: Any = None
        while len(rows) < limit:
            payload: dict[str, Any] = {
                "pageNo": 0,
                "pageSize": min(100, limit - len(rows)),
                "languageType": LANGUAGE_TYPE_ZH_CN,
                "sign": "SIGN",
                "timeZone": "",
            }
            if next_key is not None:
                payload["nextKey"] = next_key
            response = self._post(ORDER_HISTORY_PATH, payload)
            page_rows, data = _data_page(response, "Demo Web order-history query")
            added = 0
            for row in page_rows:
                order_id = _order_id(row)
                if order_id and order_id not in seen:
                    seen.add(order_id)
                    rows.append(row)
                    added += 1
                    if len(rows) >= limit:
                        break
            candidate = data.get("nextKey")
            if not data.get("nextFlag") or added == 0 or candidate in (None, "", next_key):
                break
            next_key = candidate
        if symbol is None:
            return rows
        return [row for row in rows if _matches_symbol(row, symbol)]

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        order_id = str(order_id).strip()
        if not order_id:
            raise ValidationError("order_id is required")
        response = self._post(CANCEL_ORDER_PATH, _cancel_payload([order_id]))
        _require_success(response, "Demo Web cancel order")
        return self._verify_canceled({order_id}, response)

    def cancel_all_orders(self, symbol: str | None = None) -> dict[str, Any]:
        if symbol is not None:
            raise ValidationError(
                "Demo Web cancel-all cannot safely map a symbol to WEEX contractId; "
                "query and cancel exact order IDs instead"
            )
        before = self.open_orders()
        expected_ids = {_order_id(row) for row in before}
        expected_ids.discard("")
        response = self._post(
            CANCEL_ALL_PATH,
            {
                "languageType": LANGUAGE_TYPE_ZH_CN,
                "sign": "SIGN",
                "timeZone": "",
                "filterCoinIdList": [DEMO_COIN_ID],
                "filterLegacyOrderDirectionList": ["OPEN_LONG", "OPEN_SHORT", "CLOSE_LONG", "CLOSE_SHORT"],
                "filterOrderStatusList": ["OPEN", "PENDING"],
                "filterContractIdList": [],
            },
        )
        _require_success(response, "Demo Web cancel all orders")
        return self._verify_canceled(expected_ids, response)

    def _verify_canceled(self, expected_ids: set[str], response: Mapping[str, Any]) -> dict[str, Any]:
        try:
            remaining = {_order_id(row) for row in self.open_orders()}
        except Exception as exc:  # noqa: BLE001 - mutation succeeded but verification state is unknown
            return {
                "status": "uncertain",
                "requested_order_ids": sorted(expected_ids),
                "reason": f"cancel accepted but open-order verification failed: {type(exc).__name__}",
                "response_code": response.get("code"),
            }
        unresolved = sorted(expected_ids & remaining)
        return {
            "status": "verified_canceled" if not unresolved else "cancel_pending",
            "requested_order_ids": sorted(expected_ids),
            "remaining_order_ids": unresolved,
            "response_code": response.get("code"),
        }

    def _post(self, path: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        credentials = self.settings.require_web_credentials()
        timestamp = self._now_ms()
        vs = self._random_text(32)
        headers = _headers(credentials, timestamp, vs)
        return self._transport(
            f"{WEB_BASE_URL}{path}",
            headers,
            payload,
            self.settings.timeout_ms / 1000,
        )


def web_signature(timestamp: int, vs: str, terminal_code: str) -> str:
    source = f"weex{timestamp}{vs}{TERMINAL_TYPE}{APP_VERSION}{terminal_code}"
    return hashlib.md5(source.encode()).hexdigest()  # noqa: S324 - protocol-mandated WEEX request signature


def _headers(credentials: WebCredentials, timestamp: int, vs: str) -> dict[str, str]:
    return {
        "Content-Type": "application/json;charset=utf-8",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
        ),
        "U-TOKEN": credentials.cc_token,
        "terminaltype": TERMINAL_TYPE,
        "appVersion": APP_VERSION,
        "vs": vs,
        "terminalCode": credentials.terminal_code,
        "X-TIMESTAMP": str(timestamp),
        "X-SIG": web_signature(timestamp, vs, credentials.terminal_code),
        "traceId": str(uuid.uuid4()),
        "bundleid": "",
        "X-Origin": "https://www.weex.com",
        "Origin": "https://www.weex.com",
        "Referer": "https://www.weex.com/",
        "language": "zh_CN",
        "locale": "zh_CN",
        "sidecar": "",
    }


def _urllib_post(
    url: str,
    headers: Mapping[str, str],
    payload: Mapping[str, Any],
    timeout: float,
) -> Mapping[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, separators=(",", ":")).encode(),
        headers=dict(headers),
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed HTTPS host
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise SubmissionUncertainError(f"WEEX Demo Web request outcome is uncertain: HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise SubmissionUncertainError(f"WEEX Demo Web request outcome is uncertain: {type(exc).__name__}") from exc
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValidationError("WEEX Demo Web returned invalid JSON") from exc
    if not isinstance(decoded, Mapping):
        raise ValidationError("WEEX Demo Web returned a non-object response")
    return decoded


def _data_page(response: Mapping[str, Any], operation: str) -> tuple[list[dict[str, Any]], Mapping[str, Any]]:
    _require_success(response, operation)
    data = response.get("data")
    if not isinstance(data, Mapping):
        raise ValidationError(f"{operation} response has no data object")
    rows = data.get("dataList", [])
    if not isinstance(rows, list) or any(not isinstance(row, Mapping) for row in rows):
        raise ValidationError(f"{operation} response has invalid dataList")
    return [dict(row) for row in rows], data


def _require_success(response: Mapping[str, Any], operation: str) -> None:
    code = response.get("code")
    if code is not None and str(code).upper() not in SUCCESS_CODES:
        message = str(response.get("msg") or response.get("message") or "unknown exchange error")
        raise ValidationError(f"{operation} failed with code {code}: {message}")


def _cancel_payload(order_ids: list[str]) -> dict[str, Any]:
    return {
        "languageType": LANGUAGE_TYPE_ZH_CN,
        "sign": "SIGN",
        "timeZone": "",
        "orderIdList": order_ids,
    }


def _order_id(row: Mapping[str, Any]) -> str:
    return str(row.get("id") or row.get("orderId") or "")


def _matches_symbol(row: Mapping[str, Any], symbol: str) -> bool:
    target = symbol.upper().replace("/", "").replace("-", "")
    candidates = {
        str(row.get(key) or "").upper().replace("/", "").replace("-", "")
        for key in ("symbol", "contractName", "displayContractName", "productCode")
    }
    candidates.discard("")
    return target in candidates or any(target.startswith(value) or value.startswith(target) for value in candidates)


def _random_text(length: int) -> str:
    alphabet = string.ascii_letters + string.digits
    value = [secrets.choice(alphabet) for _ in range(length)]
    for index, character in ((1, "5"), (3, "7"), (7, "8"), (14, "9"), (20, "7"), (29, "6")):
        if index < length:
            value[index] = character
    return "".join(value)
