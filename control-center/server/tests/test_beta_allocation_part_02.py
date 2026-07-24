import asyncio

import httpx
import pytest

from fleet_api.beta_allocation import HttpBetaAllocationProvider
from fleet_api.config import ControlPlaneSettings
from fleet_api.execution import AllocationUnavailable

from .test_beta_allocation_support import (
    RATIO_URL,
    account_context,
    healthy_payload,
)


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
