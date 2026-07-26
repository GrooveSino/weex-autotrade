"""Compatibility import point for the live Typer application."""

from __future__ import annotations

from . import beta_campaign as _beta_campaign  # noqa: F401
from . import beta_volume as _beta_volume  # noqa: F401
from . import maker_volume as _maker_volume  # noqa: F401
from .app import app

__all__ = ["app"]
