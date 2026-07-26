"""Compatibility hooks for test seams around external execution primitives."""

from __future__ import annotations

import sys
from typing import Any

from weex_cli.execution.adaptive import execute_adaptive_maker_target as _execute_adaptive_maker_target
from weex_cli.execution.dust_position_close import close_dust_position_once as _close_dust_position_once


def execute_maker_target(*args: Any, **kwargs: Any) -> Any:
    package = sys.modules.get("weex_cli.beta_volume")
    execute = getattr(package, "execute_adaptive_maker_target", _execute_adaptive_maker_target)
    return execute(*args, **kwargs)


def close_dust_position(*args: Any, **kwargs: Any) -> Any:
    package = sys.modules.get("weex_cli.beta_volume")
    close = getattr(package, "close_dust_position_once", _close_dust_position_once)
    return close(*args, **kwargs)
