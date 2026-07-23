

import httpx

from fleet_api.beta_allocation import HttpBetaAllocationProvider
from fleet_api.models import AccountInstance, InstanceStatus, ProxySnapshot, ProxyType, TradingMode
from fleet_api.telemetry import AccountTelemetryContext

RATIO_URL = "https://ratio.example.test/api/v1/hedge-ratio"

def healthy_payload() -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "status": "ok",
        "usable": True,
        "reason_codes": [],
        "strategy": "btc_long_eth_short",
        "as_of": 1784370658.59,
        "generated_at": 1784370658.69,
        "age_ms": 96,
        "max_age_ms": 10000,
        "ratio": {"btc_long": 1.0, "eth_short": 0.2055, "beta": 0.2055},
        "allocation": {"btc_long_weight": 0.8295, "eth_short_weight": 0.1705},
        "confidence": 0.75,
        "confidence_threshold": 0.65,
        "source": "beta_v2",
    }

def account_context(instance_id: str = "ins-beta") -> AccountTelemetryContext:
    return AccountTelemetryContext(
        AccountInstance(
            id=instance_id,
            name="Beta account",
            account_tag="beta",
            api_key_tail="ABCD",
            mode=TradingMode.DEMO,
            status=InstanceStatus.RUNNING,
            phase="运行中",
            proxy=ProxySnapshot(type=ProxyType.HTTPS, host="proxy.example.com:9000"),
        ),
        None,
    )

async def allocation_from_response(response: httpx.Response):
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: response))
    provider = HttpBetaAllocationProvider(
        RATIO_URL,
        timeout_seconds=3,
        cache_seconds=1,
        client=client,
    )
    try:
        return await provider.get(account_context())
    finally:
        await provider.aclose()
        await client.aclose()

async def snapshot_from_payload(payload: dict[str, object]):
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload, request=request))
    )
    provider = HttpBetaAllocationProvider(
        RATIO_URL,
        timeout_seconds=3,
        cache_seconds=1,
        client=client,
    )
    try:
        return await provider.market_snapshot()
    finally:
        await provider.aclose()
        await client.aclose()
