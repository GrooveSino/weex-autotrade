"""Typer/Click localization hooks kept separate from translation content."""

from __future__ import annotations

import inspect
from typing import Any, get_type_hints

from .language import current_language, text
from .translation import translate_help, translate_message


def localize_typer_app(app: Any) -> None:
    if current_language() != "zh":
        return
    _translate_attr(getattr(app, "info", None), "help")
    callback_info = getattr(app, "registered_callback", None)
    if callback_info is not None:
        _translate_callback(getattr(callback_info, "callback", None))
    for command in getattr(app, "registered_commands", ()):
        _translate_attr(command, "help")
        _translate_attr(command, "rich_help_panel")
        _translate_callback(getattr(command, "callback", None))
    for group in getattr(app, "registered_groups", ()):
        _translate_attr(group, "help")
        _translate_attr(group, "rich_help_panel")
        nested = getattr(group, "typer_instance", None)
        if nested is not None:
            localize_typer_app(nested)


def _translate_callback(callback: Any) -> None:
    if callback is None:
        return
    if isinstance(getattr(callback, "__doc__", None), str):
        callback.__doc__ = translate_help(inspect.cleandoc(callback.__doc__))
    try:
        resolved_hints = get_type_hints(callback, include_extras=True)
    except (NameError, TypeError):
        resolved_hints = {}
    for parameter in inspect.signature(callback).parameters.values():
        candidates = [parameter.default]
        annotation = resolved_hints.get(parameter.name, parameter.annotation)
        candidates.extend(getattr(annotation, "__metadata__", ()))
        for candidate in candidates:
            _translate_attr(candidate, "help")
            _translate_attr(candidate, "rich_help_panel")


def _translate_attr(target: Any, name: str) -> None:
    if target is None:
        return
    value = getattr(target, name, None)
    if isinstance(value, str):
        setattr(target, name, translate_help(value))


_TYPER_PATCHED = False


def install_typer_i18n() -> None:
    global _TYPER_PATCHED
    if _TYPER_PATCHED:
        configure_typer_constants()
        return
    import typer.core
    import typer.main
    import typer.rich_utils
    from typer._click import decorators, exceptions, formatting

    typer.core._ = _framework_text
    typer.rich_utils._ = _framework_text
    original_write_usage = formatting.HelpFormatter.write_usage

    def write_usage(self: Any, prog: str, args: str = "", prefix: str | None = None) -> None:
        original_write_usage(self, prog, args, text("用法：", "Usage: ") if prefix is None else prefix)

    formatting.HelpFormatter.write_usage = write_usage
    original_help_option = decorators.help_option

    def help_option(param_decls: list[str]) -> Any:
        decorate = original_help_option(param_decls)

        def apply(command: Any) -> Any:
            result = decorate(command)
            if result.params:
                result.params[-1].help = text("显示帮助信息并退出。", "Show this message and exit.")
            return result

        return apply

    decorators.help_option = help_option
    original_get_click_param = typer.main.get_click_param

    def get_click_param(param: Any) -> tuple[Any, Any]:
        click_param, convertor = original_get_click_param(param)
        if isinstance(getattr(click_param, "help", None), str):
            click_param.help = translate_help(click_param.help)
        if isinstance(getattr(click_param, "rich_help_panel", None), str):
            click_param.rich_help_panel = translate_help(click_param.rich_help_panel)
        return click_param, convertor

    typer.main.get_click_param = get_click_param
    for exception_type in _LOCALIZED_EXCEPTIONS(exceptions):
        original = exception_type.format_message

        def localized_format(self: Any, _original: Any = original) -> str:
            return translate_message(_original(self))

        exception_type.format_message = localized_format
    _TYPER_PATCHED = True
    configure_typer_constants()


def _LOCALIZED_EXCEPTIONS(exceptions: Any) -> tuple[type[Any], ...]:
    return (
        exceptions.ClickException,
        exceptions.UsageError,
        exceptions.BadParameter,
        exceptions.MissingParameter,
        exceptions.NoSuchOption,
        exceptions.BadOptionUsage,
        exceptions.BadArgumentUsage,
    )


def _framework_text(value: str) -> str:
    if current_language() != "zh":
        return value
    return _FRAMEWORK_ZH.get(value, value)


_FRAMEWORK_ZH = {
    "Arguments": "参数",
    "Options": "选项",
    "Commands": "命令",
    "Error": "错误",
    "Missing command.": "缺少命令。",
    "Aborted.": "已中止。",
    "(deprecated) ": "（已弃用）",
    "[default: {}]": "[默认值：{}]",
    "[env var: {}]": "[环境变量：{}]",
    "[required]": "[必填]",
    "Try [blue]'{command_path} {help_option}'[/] for help.": "运行 [blue]'{command_path} {help_option}'[/] 查看帮助。",
}


def configure_typer_constants() -> None:
    try:
        import typer.rich_utils as rich_utils
    except ImportError:
        return
    rich_utils.DEPRECATED_STRING = _framework_text("(deprecated) ")
    rich_utils.DEFAULT_STRING = _framework_text("[default: {}]")
    rich_utils.ENVVAR_STRING = _framework_text("[env var: {}]")
    rich_utils.REQUIRED_LONG_STRING = _framework_text("[required]")
    rich_utils.ARGUMENTS_PANEL_TITLE = _framework_text("Arguments")
    rich_utils.OPTIONS_PANEL_TITLE = _framework_text("Options")
    rich_utils.COMMANDS_PANEL_TITLE = _framework_text("Commands")
    rich_utils.ERRORS_PANEL_TITLE = _framework_text("Error")
    rich_utils.ABORTED_TEXT = _framework_text("Aborted.")
    rich_utils.RICH_HELP = _framework_text("Try [blue]'{command_path} {help_option}'[/] for help.")
