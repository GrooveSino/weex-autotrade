"""Language selection state for the command-line presentation layer."""

from __future__ import annotations

import os
import sys
from contextvars import ContextVar

DEFAULT_LANGUAGE = "zh"
SUPPORTED_LANGUAGES = {"zh", "en"}


def _detect_language() -> str:
    selected = os.environ.get("WEEX_CLI_LANG", DEFAULT_LANGUAGE)
    for index, argument in enumerate(sys.argv):
        if argument == "--lang" and index + 1 < len(sys.argv):
            selected = sys.argv[index + 1]
            break
        if argument.startswith("--lang="):
            selected = argument.partition("=")[2]
            break
    return _normalise_or_default(selected)


def _normalise_or_default(value: str) -> str:
    return _LANGUAGE_ALIASES.get(value.strip().lower().replace("_", "-"), DEFAULT_LANGUAGE)


_LANGUAGE_ALIASES = {
    "zh": "zh",
    "zh-cn": "zh",
    "zh-hans": "zh",
    "cn": "zh",
    "en": "en",
    "en-us": "en",
    "en-gb": "en",
}
_initial_language = _detect_language()
_language: ContextVar[str | None] = ContextVar("weex_cli_language", default=None)


def current_language() -> str:
    return _language.get() or _initial_language


def set_language(value: str) -> str:
    selected = _LANGUAGE_ALIASES.get(value.strip().lower().replace("_", "-"))
    if selected is None:
        raise ValueError("--lang 仅支持 zh 或 en")
    _language.set(selected)
    return selected


def text(zh: str, en: str) -> str:
    return zh if current_language() == "zh" else en
