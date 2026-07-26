"""Public localization interface for the CLI presentation layer."""

from .language import DEFAULT_LANGUAGE, SUPPORTED_LANGUAGES, current_language, text
from .language import set_language as _set_language
from .translation import localize_payload, translate_field, translate_help, translate_message, translate_value
from .typer import configure_typer_constants, install_typer_i18n, localize_typer_app


def set_language(value: str) -> str:
    """Select a language and refresh Typer's process-level labels."""
    selected = _set_language(value)
    configure_typer_constants()
    return selected


__all__ = [
    "DEFAULT_LANGUAGE",
    "SUPPORTED_LANGUAGES",
    "current_language",
    "install_typer_i18n",
    "localize_payload",
    "localize_typer_app",
    "set_language",
    "text",
    "translate_field",
    "translate_help",
    "translate_message",
    "translate_value",
]
