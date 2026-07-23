"""Shared state for the Fleet ASGI composition root and route modules."""

from types import SimpleNamespace


class FleetAppContext(SimpleNamespace):
    """Explicit application dependencies, kept outside route-module globals."""
