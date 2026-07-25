"""Execution contracts and runtime resources."""

from importlib import import_module

_PUBLIC = "fleet_api.execution.contracts.public"


def __getattr__(name: str):
    value = getattr(import_module(_PUBLIC), name)
    globals()[name] = value
    return value
