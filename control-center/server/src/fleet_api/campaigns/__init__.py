"""Campaign domain package."""

from importlib import import_module

_PUBLIC = "fleet_api.campaigns.persistence.campaigns"


def __getattr__(name: str):
    value = getattr(import_module(_PUBLIC), name)
    globals()[name] = value
    return value
