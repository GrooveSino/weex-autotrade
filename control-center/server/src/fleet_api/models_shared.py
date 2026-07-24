from __future__ import annotations

import time
from decimal import Decimal
from enum import StrEnum
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr
from pydantic.alias_generators import to_camel
from pydantic.functional_validators import model_validator

class CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, serialize_by_alias=True, extra="forbid")


class TradingMode(StrEnum):
    DEMO = "demo"
    LIVE = "live"


class InstanceStatus(StrEnum):
    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"
    WARNING = "warning"
    ERROR = "error"


class ProxyType(StrEnum):
    NONE = "none"
    HTTP = "http"
    HTTPS = "https"
    SOCKS5 = "socks5"


class ProxyStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNCHECKED = "unchecked"


class LogLevel(StrEnum):
    INFO = "info"
    SUCCESS = "success"
    WARN = "warn"
    ERROR = "error"


class InstanceAction(StrEnum):
    START = "start"
    PAUSE = "pause"
    STOP = "stop"


class StrategyStage(StrEnum):
    IDLE = "idle"
    HOLDING = "holding"
    COOLDOWN = "cooldown"
    COMPLETE = "complete"


class StrategyTargetMode(StrEnum):
    INCREMENTAL = "incremental"
    LIFETIME = "lifetime"


class StrategyDirection(StrEnum):
    BTC_LONG_ETH_SHORT = "btc_long_eth_short"
    BTC_SHORT_ETH_LONG = "btc_short_eth_long"


class FundingPreflightStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    INSUFFICIENT = "insufficient"
