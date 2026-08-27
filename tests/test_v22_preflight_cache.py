import json
from uuid import UUID

import pytest

from app.api.v2.models import ModuleAvailability, PreflightRequest, PreflightResponse
from app.preflight_v22.cache import PreflightCache


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.set_calls: list[tuple[str, str, int]] = []
        self.fail_get = False
        self.fail_set = False

    async def get(self, key: str):
        if self.fail_get:
            raise OSError("redis unavailable")
        return self.values.get(key)

    async def set(self, key: str, value: str, *, ex: int):
        if self.fail_set:
            raise OSError("redis unavailable")
        self.values[key] = value
        self.set_calls.append((key, value, ex))


def request_payload() -> PreflightRequest:
    return PreflightRequest.model_validate_json(json.dumps({
        "site_url": "https://example.com/path",
        "primary_service": "Plumbing",
        "target_market": {
            "display_name": "Austin, TX, US",
            "country_code": "US",
            "region": "TX",
            "city": "Austin",
        },
    }))


def response_payload() -> PreflightResponse:
    return PreflightResponse(
        preflight_id=UUID("11111111-1111-4111-8111-111111111111"),
        normalized_site_url="https://example.com/",
        module_availability=[
            ModuleAvailability(module_key="site_inventory", available=True, reason="Homepage available."),
        ],
        estimated_duration_bucket="under_5_minutes",
        coverage_summary="One public site module is available for later collection.",
    )


def test_cache_key_is_stable_and_versioned() -> None:
    cache = PreflightCache(None, prefix=" searchtrust:v22 ", ttl_seconds=900)

    first = cache.key(request_payload(), "https://example.com/")
    second = cache.key(request_payload(), "https://example.com/")

    assert first == second
    assert first.startswith("searchtrust:v22:preflight:v1:2.2.0:")
    assert "Plumbing" not in first


@pytest.mark.anyio
async def test_cache_round_trip_revalidates_frozen_response() -> None:
    redis = FakeRedis()
    cache = PreflightCache(redis, prefix="searchtrust:v22", ttl_seconds=900)
    key = cache.key(request_payload(), "https://example.com/")

    await cache.set(key, response_payload())
    result = await cache.get(key)

    assert result == response_payload()
    assert redis.set_calls[0][2] == 900


@pytest.mark.anyio
async def test_cache_ignores_bad_payload_and_redis_failures() -> None:
    redis = FakeRedis()
    cache = PreflightCache(redis, prefix="searchtrust:v22", ttl_seconds=900)
    key = cache.key(request_payload(), "https://example.com/")
    redis.values[key] = '{"invalid":true}'

    assert await cache.get(key) is None

    redis.fail_get = True
    assert await cache.get(key) is None

    redis.fail_get = False
    redis.fail_set = True
    await cache.set(key, response_payload())
