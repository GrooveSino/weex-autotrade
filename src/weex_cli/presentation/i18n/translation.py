"""Translate CLI labels, payloads, and user-facing errors."""

from __future__ import annotations

import re
from typing import Any

from .catalog import _ERROR_EXACT_ZH, _FIELD_ZH, _HELP_ZH, _REASON_ZH, _VALUE_ZH
from .language import current_language


def translate_help(value: str) -> str:
    return _HELP_ZH.get(value, value) if current_language() == "zh" else value


def translate_value(value: Any) -> str:
    rendered = str(value)
    if current_language() != "zh":
        return rendered
    normalized = rendered.strip().lower()
    return _VALUE_ZH.get(normalized, _REASON_ZH.get(normalized, rendered))


def translate_field(value: str) -> str:
    if current_language() != "zh":
        return value.replace("_", " ").replace("positionSide", "position side").title()
    return _FIELD_ZH.get(value, value.replace("_", " "))


def localize_payload(value: Any) -> Any:
    if current_language() != "zh":
        return value
    if isinstance(value, dict):
        return {translate_field(str(key)): localize_payload(item) for key, item in value.items()}
    if isinstance(value, list):
        return [localize_payload(item) for item in value]
    if isinstance(value, tuple):
        return tuple(localize_payload(item) for item in value)
    if isinstance(value, str):
        return translate_value(value)
    return value


def translate_message(message: object) -> str:
    rendered = str(message)
    if current_language() != "zh":
        return rendered
    if rendered in _ERROR_EXACT_ZH:
        return _ERROR_EXACT_ZH[rendered]
    confirmation = re.fullmatch(r"confirmation mismatch; expected exactly: (.+)", rendered)
    if confirmation:
        return f"确认短语不匹配；必须完整输入：{confirmation.group(1)}"
    suggestions = re.fullmatch(r"No such option: (.+?) \(Possible options: (.+)\)", rendered)
    if suggestions:
        return f"不存在该选项：{suggestions.group(1)}（可能的选项：{suggestions.group(2)}）"
    no_option = re.fullmatch(r"No such option: (.+)", rendered)
    if no_option:
        return f"不存在该选项：{no_option.group(1)}"
    invalid_for = re.fullmatch(r"Invalid value for (.+?): (.+)", rendered)
    if invalid_for:
        return f"参数 {invalid_for.group(1)} 的值无效：{translate_message(invalid_for.group(2))}"
    invalid = re.fullmatch(r"Invalid value: (.+)", rendered)
    if invalid:
        return f"参数值无效：{translate_message(invalid.group(1))}"
    missing = re.fullmatch(r"Missing (argument|option|parameter)(.*)", rendered)
    if missing:
        kind = {"argument": "参数", "option": "选项", "parameter": "参数"}[missing.group(1)]
        return f"缺少{kind}{missing.group(2)}"
    required = re.fullmatch(r"(.+) is required", rendered)
    if required:
        return f"缺少必填项：{required.group(1)}"
    must_be = re.fullmatch(r"(.+) must be (.+)", rendered)
    if must_be:
        condition = _MUST_BE_CONDITIONS.get(must_be.group(2), must_be.group(2))
        connector = "" if condition.startswith(("大于", "小于")) else "是 "
        return f"{must_be.group(1)} 必须{connector}{condition}"
    already_flat = re.fullmatch(r"(.+) is already flat\. No order was submitted\.", rendered)
    if already_flat:
        return f"{already_flat.group(1)} 已经处于空仓状态，未提交任何订单。"
    not_found = re.fullmatch(r"(.+) not found: (.+)", rendered, flags=re.IGNORECASE)
    if not_found:
        return f"未找到{not_found.group(1)}：{not_found.group(2)}"
    return _REASON_ZH.get(rendered, rendered)


_MUST_BE_CONDITIONS = {
    "buy or sell": "buy 或 sell",
    "demo or live": "demo 或 live",
    "greater than stop-loss": "大于止损价",
    "less than stop-loss": "小于止损价",
    "LONG or SHORT": "LONG 或 SHORT",
    "zero or greater": "大于等于 0",
}
