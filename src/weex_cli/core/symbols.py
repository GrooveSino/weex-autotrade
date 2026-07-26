from __future__ import annotations

import re

from weex_cli.core.errors import ValidationError

_BASE_RE = re.compile(r"^[A-Z0-9]{2,20}$")


def base_asset(symbol: str) -> str:
    text = str(symbol or "").strip().upper()
    if not text:
        raise ValidationError("symbol is required")
    if "/" in text:
        text = text.split("/", 1)[0]
    else:
        text = text.replace("-", "").replace("_", "").replace(":", "")
        if text.endswith("SUSDT"):
            text = text[:-5]
        elif text.endswith("USDT"):
            text = text[:-4]
    if not _BASE_RE.fullmatch(text):
        raise ValidationError(f"unsupported symbol format: {symbol}")
    return text


def live_symbol_id(symbol: str) -> str:
    return f"{base_asset(symbol)}USDT"


def demo_symbol_id(symbol: str) -> str:
    return f"{base_asset(symbol)}SUSDT"


def ccxt_swap_symbol(symbol: str) -> str:
    return f"{base_asset(symbol)}/USDT:USDT"
