"""WEEX Fleet control plane."""

from typing import Any

__all__ = ["create_app"]


def create_app(*args: Any, **kwargs: Any) -> Any:
    """Load the executor application factory without side effects on package import."""
    from .main import create_app as factory

    return factory(*args, **kwargs)
