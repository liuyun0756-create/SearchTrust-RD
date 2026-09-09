from datetime import datetime, timedelta, timezone

import fakeredis.aioredis
import pytest

from app.jobs_v22.circuit_breaker import RedisCircuitBreaker
from app.jobs_v22.errors import ProviderCircuitOpen


NOW = datetime(2026, 9, 9, 10, 0, tzinfo=timezone.utc)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_fifth_eligible_failure_opens_shared_circuit() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    breaker = RedisCircuitBreaker(redis, prefix="test:v22")

    for offset in range(5):
        permit = await breaker.before_call("serpapi", "maps", now=NOW)
        opened = await breaker.record_failure(permit, now=NOW + timedelta(seconds=offset))

    assert opened is True
    with pytest.raises(ProviderCircuitOpen):
        await breaker.before_call("serpapi", "maps", now=NOW + timedelta(seconds=10))


@pytest.mark.anyio
async def test_half_open_allows_one_probe_and_success_closes() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    breaker = RedisCircuitBreaker(redis, prefix="test:v22", failure_threshold=1)
    permit = await breaker.before_call("dify", "copy", now=NOW)
    await breaker.record_failure(permit, now=NOW)

    probe = await breaker.before_call("dify", "copy", now=NOW + timedelta(seconds=61))
    assert probe.probe is True
    with pytest.raises(ProviderCircuitOpen):
        await breaker.before_call("dify", "copy", now=NOW + timedelta(seconds=61))

    await breaker.record_success(probe)
    assert (await breaker.before_call("dify", "copy", now=NOW + timedelta(seconds=62))).probe is False


@pytest.mark.anyio
async def test_ineligible_failure_does_not_count_and_key_can_open_immediately() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    breaker = RedisCircuitBreaker(redis, prefix="test:v22", failure_threshold=2)
    permit = await breaker.before_call("serpapi:key-abc", "search", now=NOW)
    assert not await breaker.record_failure(permit, now=NOW, eligible=False)
    permit = await breaker.before_call("serpapi:key-abc", "search", now=NOW)
    assert await breaker.record_failure(permit, now=NOW, immediate=True)
    with pytest.raises(ProviderCircuitOpen):
        await breaker.before_call("serpapi:key-abc", "search", now=NOW + timedelta(seconds=1))
