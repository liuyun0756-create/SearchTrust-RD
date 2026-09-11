from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from redis.asyncio import Redis
from redis.exceptions import WatchError

from app.jobs_v22.errors import JobLeaseLost
from app.jobs_v22.keys import JobRedisKeys
from app.jobs_v22.store import DurableJobStore


pytestmark = [pytest.mark.redis_integration, pytest.mark.anyio]

JOB_ID = UUID("55555555-5555-4555-8555-555555555555")
CASE_ID = UUID("11111111-1111-4111-8111-111111111111")
NOW = datetime(2026, 9, 11, 8, 0, tzinfo=timezone.utc)
REQUEST = {"case_id": str(CASE_ID), "report_type": "prospect", "queries": ["a", "b"]}


async def _register(store: DurableJobStore):
    return await store.register_job(
        job_id=JOB_ID,
        case_id=CASE_ID,
        idempotency_key="real-redis-intent",
        request_payload=REQUEST,
        now=NOW,
    )


async def test_real_redis_watch_conflict_ttl_sorted_sets_pubsub_and_bytes(
    redis_client: Redis,
    redis_prefix: str,
) -> None:
    conflict_key = f"{redis_prefix}:watch"
    async with redis_client.pipeline(transaction=True) as pipe:
        await pipe.watch(conflict_key)
        await redis_client.set(conflict_key, b"raced", ex=30)
        pipe.multi()
        pipe.set(conflict_key, b"stale")
        with pytest.raises(WatchError):
            await pipe.execute()

    ttl = await redis_client.ttl(conflict_key)
    assert 1 <= ttl <= 30

    sorted_key = f"{redis_prefix}:sorted"
    await redis_client.zadd(sorted_key, {"later": 2, "first": 1})
    assert await redis_client.zrange(sorted_key, 0, -1) == [b"first", b"later"]

    channel = f"{redis_prefix}:events"
    async with redis_client.pubsub() as subscriber:
        await subscriber.subscribe(channel)
        acknowledgement = await subscriber.get_message(timeout=1)
        assert acknowledgement is not None and acknowledgement["type"] == "subscribe"
        assert await redis_client.publish(channel, b"7") == 1
        message = await subscriber.get_message(ignore_subscribe_messages=True, timeout=1)
        assert message is not None
        assert message["data"] == b"7"


async def test_real_store_decodes_bytes_tracks_ttl_and_sorted_sets(
    redis_client: Redis,
    redis_prefix: str,
    real_store: DurableJobStore,
) -> None:
    created = await _register(real_store)
    keys = JobRedisKeys(redis_prefix)

    assert await real_store.get_state(JOB_ID) == created.state
    assert await real_store.get_request(JOB_ID) == REQUEST
    assert await real_store.list_stale_jobs(NOW + timedelta(seconds=1)) == [JOB_ID]
    assert await real_store.list_pending_callbacks() == [JOB_ID]
    assert 1 <= await redis_client.ttl(keys.state(JOB_ID)) <= 120
    assert 1 <= await redis_client.ttl(keys.request(JOB_ID)) <= 120


async def test_concurrent_registration_has_one_idempotent_identity(
    real_store: DurableJobStore,
) -> None:
    results = await asyncio.gather(*(_register(real_store) for _ in range(20)))

    assert sum(not result.replayed for result in results) == 1
    assert {result.state.job_id for result in results} == {JOB_ID}
    assert await real_store.active_count() == 1


async def test_concurrent_transitions_have_unique_monotonic_revisions(
    real_store: DurableJobStore,
) -> None:
    await _register(real_store)

    results = await asyncio.gather(
        *(
            real_store.transition(
                    JOB_ID,
                    status="running",
                    stage="collecting_site",
                progress=index,
                message=f"Step {index}",
                now=NOW + timedelta(seconds=index),
                expected_generation=1,
            )
            for index in range(1, 21)
        )
    )

    assert all(result.applied for result in results)
    assert {result.state.revision for result in results} == set(range(2, 22))
    assert (await real_store.require_state(JOB_ID)).revision == 21


async def test_concurrent_takeover_fences_the_stale_generation(
    real_store: DurableJobStore,
) -> None:
    await _register(real_store)
    takeovers = await asyncio.gather(
        *(
            real_store.take_over_stale(
                JOB_ID,
                expected_generation=1,
                now=NOW + timedelta(minutes=4, microseconds=index),
            )
            for index in range(8)
        )
    )

    assert sum(result.applied for result in takeovers) == 1
    current = await real_store.require_state(JOB_ID)
    assert current.run_generation == 2
    assert current.revision == 2

    with pytest.raises(JobLeaseLost):
        await real_store.transition(
            JOB_ID,
            status="running",
            stage="old-worker",
            progress=1,
            message="Old worker",
            now=NOW + timedelta(minutes=5),
            expected_generation=1,
        )
