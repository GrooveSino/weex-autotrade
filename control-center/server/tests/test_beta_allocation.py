import asyncio
from decimal import Decimal, localcontext

import httpx
import pytest

from fleet_api.beta_allocation import HttpBetaAllocationProvider
from fleet_api.config import ControlPlaneSettings
from fleet_api.execution import AllocationUnavailable
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


def test_healthy_response_generates_authoritative_decimal_weights() -> None:
    allocation = asyncio.run(allocation_from_response(httpx.Response(200, json=healthy_payload())))

    with localcontext() as context:
        context.prec = 50
        expected_btc = Decimal(1) / (Decimal(1) + Decimal("0.2055"))
        expected_eth = Decimal(1) - expected_btc
    assert allocation.btc_weight == expected_btc
    assert allocation.eth_weight == expected_eth
    assert allocation.btc_weight + allocation.eth_weight == Decimal(1)
    assert allocation.version == "beta-v1:1784370658590"


def test_market_snapshot_exposes_final_beta_even_when_upstream_marks_it_unusable() -> None:
    payload = healthy_payload()
    payload.update({"status": "low_confidence", "usable": False, "reason_codes": ["confidence_below_threshold"]})
    payload["confidence"] = 0.50

    snapshot = asyncio.run(snapshot_from_payload(payload))

    assert snapshot.final_beta == Decimal("0.2055")
    assert snapshot.upstream_usable is False
    assert snapshot.status == "low_confidence"
    assert snapshot.reason_codes == ["confidence_below_threshold"]
    assert snapshot.btc_long_weight == Decimal("0.8295")
    assert snapshot.eth_short_weight == Decimal("0.1705")


def test_status_ok_but_low_confidence_is_accepted_for_execution() -> None:
    payload = healthy_payload()
    payload["confidence"] = 0.60

    allocation = asyncio.run(allocation_from_response(httpx.Response(200, json=payload)))

    assert allocation.eth_weight / allocation.btc_weight == Decimal("0.2055")


def test_low_confidence_status_and_unusable_flag_are_accepted_for_execution() -> None:
    payload = healthy_payload()
    payload.update({"status": "low_confidence", "usable": False, "confidence": 0.25})

    allocation = asyncio.run(allocation_from_response(httpx.Response(200, json=payload)))

    assert allocation.version == "beta-v1:1784370658590"


@pytest.mark.parametrize(
    ("payload_update", "reason_code"),
    [
        ({"status": "stale", "usable": False}, "beta_status_stale"),
        ({"status": "unavailable", "usable": False}, "beta_status_unavailable"),
        ({"schema_version": "2.0"}, "beta_schema_version"),
        ({"strategy": "different_strategy"}, "beta_strategy"),
        ({"age_ms": 10001}, "beta_stale_age"),
    ],
)
def test_unusable_or_incompatible_payloads_fail_closed(
    payload_update: dict[str, object],
    reason_code: str,
) -> None:
    payload = healthy_payload()
    payload.update(payload_update)

    with pytest.raises(AllocationUnavailable) as caught:
        asyncio.run(allocation_from_response(httpx.Response(200, json=payload)))

    assert caught.value.reason_code == reason_code
    assert caught.value.args == (reason_code,)


@pytest.mark.parametrize("beta", [0, -1, "NaN", "Infinity", True, None])
def test_invalid_beta_never_generates_an_allocation(beta: object) -> None:
    payload = healthy_payload()
    payload["ratio"] = {"beta": beta}

    with pytest.raises(AllocationUnavailable, match="^beta_invalid_ratio$"):
        asyncio.run(allocation_from_response(httpx.Response(200, json=payload)))


def test_http_503_fails_closed_without_one_to_one_fallback() -> None:
    with pytest.raises(AllocationUnavailable) as caught:
        asyncio.run(allocation_from_response(httpx.Response(503, text="upstream unavailable details")))

    assert caught.value.reason_code == "beta_http_status"
    assert caught.value.args == ("beta_http_status",)


def test_timeout_fails_closed() -> None:
    async def scenario() -> None:
        async def timeout(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("sensitive transport details", request=request)

        client = httpx.AsyncClient(transport=httpx.MockTransport(timeout))
        provider = HttpBetaAllocationProvider(RATIO_URL, timeout_seconds=3, cache_seconds=1, client=client)
        try:
            with pytest.raises(AllocationUnavailable) as caught:
                await provider.get(account_context())
            assert caught.value.reason_code == "beta_timeout"
            assert caught.value.args == ("beta_timeout",)
        finally:
            await provider.aclose()
            await client.aclose()

    asyncio.run(scenario())


def test_invalid_json_fails_closed() -> None:
    with pytest.raises(AllocationUnavailable, match="^beta_invalid_json$"):
        asyncio.run(allocation_from_response(httpx.Response(200, content=b"{not-json")))


def test_concurrent_accounts_share_one_upstream_request() -> None:
    async def scenario() -> None:
        calls = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.02)
            return httpx.Response(200, json=healthy_payload(), request=request)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = HttpBetaAllocationProvider(RATIO_URL, timeout_seconds=3, cache_seconds=1, client=client)
        try:
            allocations = await asyncio.gather(
                *(provider.get(account_context(f"ins-beta-{index}")) for index in range(40))
            )
            assert calls == 1
            assert len({allocation.version for allocation in allocations}) == 1
            assert all(allocation.btc_weight + allocation.eth_weight == Decimal(1) for allocation in allocations)
        finally:
            await provider.aclose()
            await client.aclose()

    asyncio.run(scenario())


def test_centralized_refresh_distributes_one_snapshot_without_consumer_network_requests() -> None:
    async def scenario() -> None:
        calls = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, json=healthy_payload(), request=request)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = HttpBetaAllocationProvider(
            RATIO_URL,
            timeout_seconds=3,
            cache_seconds=10,
            client=client,
            network_on_demand=False,
        )
        try:
            with pytest.raises(AllocationUnavailable, match="^beta_not_ready$"):
                await provider.get(account_context())
            assert calls == 0

            assert await provider.refresh() is True
            assert 0 < provider.seconds_until_refresh(10) < 10
            allocations = await asyncio.gather(
                *(provider.get(account_context(f"ins-central-{index}")) for index in range(40))
            )
            snapshots = await asyncio.gather(*(provider.market_snapshot() for _ in range(10)))

            assert calls == 1
            assert len({allocation.version for allocation in allocations}) == 1
            assert {snapshot.final_beta for snapshot in snapshots} == {Decimal("0.2055")}
        finally:
            await provider.aclose()
            await client.aclose()

    asyncio.run(scenario())


def test_centralized_refresh_replaces_the_snapshot_used_by_the_next_cycle() -> None:
    async def scenario() -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            payload = healthy_payload()
            if calls == 2:
                payload["as_of"] = 1784370668.59
                payload["ratio"] = {"btc_long": 1.0, "eth_short": 0.5, "beta": 0.5}
                payload["allocation"] = {
                    "btc_long_weight": 0.6666666666666666,
                    "eth_short_weight": 0.3333333333333333,
                }
            return httpx.Response(200, json=payload, request=request)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = HttpBetaAllocationProvider(
            RATIO_URL,
            timeout_seconds=3,
            cache_seconds=10,
            client=client,
            network_on_demand=False,
        )
        try:
            assert await provider.refresh() is True
            first = await provider.get(account_context())
            assert await provider.refresh() is True
            second = await provider.get(account_context())

            assert calls == 2
            assert first.eth_weight / first.btc_weight == Decimal("0.2055")
            assert second.eth_weight / second.btc_weight == Decimal("0.5")
            assert first.version != second.version
        finally:
            await provider.aclose()
            await client.aclose()

    asyncio.run(scenario())


def test_centralized_low_confidence_snapshot_remains_visible_and_drives_execution() -> None:
    async def scenario() -> None:
        calls = 0
        payload = healthy_payload()
        payload.update({"status": "low_confidence", "usable": False})

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, json=payload, request=request)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = HttpBetaAllocationProvider(
            RATIO_URL,
            timeout_seconds=3,
            cache_seconds=10,
            client=client,
            network_on_demand=False,
        )
        try:
            assert await provider.refresh() is True
            snapshot = await provider.market_snapshot()
            allocation = await provider.get(account_context())

            assert calls == 1
            assert snapshot.final_beta == Decimal("0.2055")
            assert snapshot.upstream_usable is False
            assert allocation.eth_weight / allocation.btc_weight == Decimal("0.2055")
        finally:
            await provider.aclose()
            await client.aclose()

    asyncio.run(scenario())


def test_centralized_consumers_fail_stale_without_triggering_an_upstream_refresh() -> None:
    async def scenario() -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, json=healthy_payload(), request=request)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = HttpBetaAllocationProvider(
            RATIO_URL,
            timeout_seconds=3,
            cache_seconds=0.01,
            client=client,
            network_on_demand=False,
        )
        try:
            assert await provider.refresh() is True
            await asyncio.sleep(0.02)
            with pytest.raises(AllocationUnavailable, match="^beta_snapshot_stale$"):
                await provider.get(account_context())
            assert calls == 1
        finally:
            await provider.aclose()
            await client.aclose()

    asyncio.run(scenario())


def test_concurrent_accounts_share_one_failed_upstream_request() -> None:
    async def scenario() -> None:
        calls = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.02)
            return httpx.Response(503, request=request)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = HttpBetaAllocationProvider(RATIO_URL, timeout_seconds=3, cache_seconds=1, client=client)
        try:
            results = await asyncio.gather(
                *(provider.get(account_context(f"ins-beta-{index}")) for index in range(40)),
                return_exceptions=True,
            )
            assert calls == 1
            assert all(
                isinstance(result, AllocationUnavailable) and result.reason_code == "beta_http_status"
                for result in results
            )
        finally:
            await provider.aclose()
            await client.aclose()

    asyncio.run(scenario())


def test_expired_success_is_never_used_after_upstream_failure() -> None:
    async def scenario() -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls == 1:
                return httpx.Response(200, json=healthy_payload(), request=request)
            return httpx.Response(503, request=request)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = HttpBetaAllocationProvider(RATIO_URL, timeout_seconds=3, cache_seconds=0.01, client=client)
        try:
            await provider.get(account_context())
            await asyncio.sleep(0.02)
            with pytest.raises(AllocationUnavailable, match="^beta_http_status$"):
                await provider.get(account_context())
            assert calls == 2
        finally:
            await provider.aclose()
            await client.aclose()

    asyncio.run(scenario())


def test_cache_never_outlives_upstream_max_age() -> None:
    async def scenario() -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls == 1:
                payload = healthy_payload()
                payload["age_ms"] = 9950
                return httpx.Response(200, json=payload, request=request)
            return httpx.Response(503, request=request)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = HttpBetaAllocationProvider(RATIO_URL, timeout_seconds=3, cache_seconds=1, client=client)
        try:
            await provider.get(account_context())
            await asyncio.sleep(0.06)
            with pytest.raises(AllocationUnavailable, match="^beta_http_status$"):
                await provider.get(account_context())
            assert calls == 2
        finally:
            await provider.aclose()
            await client.aclose()

    asyncio.run(scenario())


def test_beta_ratio_settings_load_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FLEET_BETA_RATIO_URL", "http://127.0.0.1:5888/api/v1/hedge-ratio")
    monkeypatch.setenv("FLEET_BETA_RATIO_TIMEOUT_SECONDS", "2.5")
    monkeypatch.setenv("FLEET_BETA_REFRESH_SECONDS", "10")
    monkeypatch.setenv("FLEET_BETA_BACKGROUND_REFRESH_ENABLED", "true")

    settings = ControlPlaneSettings.load()

    assert settings.beta_ratio_url == "http://127.0.0.1:5888/api/v1/hedge-ratio"
    assert settings.beta_ratio_timeout_seconds == 2.5
    assert settings.beta_refresh_interval_seconds == 10
    assert settings.beta_background_refresh_enabled is True


def test_provider_retries_after_unavailable_cache_expires() -> None:
    async def scenario() -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls == 1:
                return httpx.Response(503, request=request)
            return httpx.Response(200, json=healthy_payload(), request=request)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = HttpBetaAllocationProvider(RATIO_URL, timeout_seconds=3, cache_seconds=0.01, client=client)
        try:
            with pytest.raises(AllocationUnavailable, match="^beta_http_status$"):
                await provider.get(account_context())
            await asyncio.sleep(0.02)
            allocation = await provider.get(account_context())
            assert allocation.version == "beta-v1:1784370658590"
            assert calls == 2
        finally:
            await provider.aclose()
            await client.aclose()

    asyncio.run(scenario())
