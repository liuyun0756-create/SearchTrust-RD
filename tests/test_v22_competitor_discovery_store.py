from __future__ import annotations

import asyncio
from datetime import timedelta
from uuid import UUID

import fakeredis.aioredis
import pytest

from app.api.v2.competitor_models import CompetitorDiscoveryError
from app.competitors_v22.store import CompetitorDiscoveryStore
from app.competitors_v22.reconciler import reconcile_competitor_discoveries_once
from app.jobs_v22.errors import IdempotencyConflict, JobIdentityConflict, JobNotRetryable
from test_v22_competitor_models import CASE_ID, JOB_ID, NOW, discovery_request, discovery_result


OTHER_JOB_ID = UUID("88888888-8888-4888-8888-888888888888")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def redis():
    return fakeredis.aioredis.FakeRedis(decode_responses=False)


@pytest.fixture
def store(redis):
    return CompetitorDiscoveryStore(redis, prefix="test:v22", state_ttl_seconds=604_800)


async def register(store, **overrides):
    values = {
        "discovery_job_id": JOB_ID,
        "case_id": CASE_ID,
        "idempotency_key": "competitor-discovery-1",
        "request_payload": discovery_request().model_dump(mode="json"),
        "now": NOW,
    }
    values.update(overrides)
    return await store.register_job(**values)


@pytest.mark.anyio
async def test_concurrent_discovery_registration_is_atomic(store) -> None:
    results = await asyncio.gather(*(register(store) for _ in range(8)))

    assert sum(not result.replayed for result in results) == 1
    assert {result.state.discovery_job_id for result in results} == {JOB_ID}
    assert await store.active_count() == 1


@pytest.mark.anyio
async def test_discovery_idempotency_and_identity_conflicts(store) -> None:
    await register(store)

    with pytest.raises(IdempotencyConflict):
        await register(
            store,
            request_payload=discovery_request(
                queries=["different plumber", "plumber near me", "24 hour plumber"]
            ).model_dump(mode="json"),
        )
    with pytest.raises(JobIdentityConflict):
        await register(store, discovery_job_id=OTHER_JOB_ID)


@pytest.mark.anyio
async def test_discovery_state_transitions_and_first_terminal_wins(store) -> None:
    await register(store)
    running = await store.transition(
        JOB_ID,
        status="running",
        stage="collecting_market",
        progress=10,
        message="Collecting market results.",
        now=NOW + timedelta(seconds=1),
        attempt_count=1,
    )
    succeeded = await store.transition(
        JOB_ID,
        status="succeeded",
        stage="completed",
        progress=100,
        message="Competitor discovery complete.",
        now=NOW + timedelta(seconds=2),
        result=discovery_result().model_copy(update={"discovery_id": JOB_ID}),
    )
    duplicate = await store.transition(
        JOB_ID,
        status="failed",
        stage="failed",
        progress=100,
        message="Should not replace success.",
        now=NOW + timedelta(seconds=3),
        error=CompetitorDiscoveryError(
            error_code="DUPLICATE_FAILURE",
            user_message="Should not replace success.",
            retryable=False,
            stage="failed",
            diagnostic_id=UUID("99999999-9999-4999-8999-999999999999"),
        ),
    )

    assert running.applied is True
    assert succeeded.applied is True
    assert duplicate.applied is False
    assert duplicate.state.status == "succeeded"
    assert await store.active_count() == 0


@pytest.mark.anyio
async def test_retry_reopens_only_retryable_failure(store) -> None:
    await register(store)
    await store.transition(
        JOB_ID,
        status="failed",
        stage="failed",
        progress=10,
        message="Temporary failure.",
        now=NOW + timedelta(seconds=1),
        error=CompetitorDiscoveryError(
            error_code="PROVIDER_TEMPORARILY_UNAVAILABLE",
            user_message="Temporary failure.",
            retryable=True,
            stage="failed",
            diagnostic_id=UUID("99999999-9999-4999-8999-999999999999"),
        ),
    )

    retried = await store.retry_failed(JOB_ID, now=NOW + timedelta(seconds=2))

    assert retried.status == "queued"
    assert retried.run_generation == 2
    assert retried.result is None
    with pytest.raises(JobNotRetryable):
        await store.retry_failed(JOB_ID, now=NOW + timedelta(seconds=3))


@pytest.mark.anyio
async def test_discovery_request_and_state_receive_seven_day_ttl(redis, store) -> None:
    await register(store)

    assert 604_790 <= await redis.ttl(store.keys.state(JOB_ID)) <= 604_800
    assert 604_790 <= await redis.ttl(store.keys.request(JOB_ID)) <= 604_800


class RecordingQueue:
    def __init__(self) -> None:
        self.calls = []

    async def enqueue(self, discovery_job_id, run_generation):
        self.calls.append((discovery_job_id, run_generation))
        return True


@pytest.mark.anyio
async def test_stale_discovery_is_requeued_for_recovery(store) -> None:
    await register(store)
    queue = RecordingQueue()

    await reconcile_competitor_discoveries_once(
        store=store,
        queue=queue,
        now=NOW + timedelta(minutes=10),
        stale_seconds=180,
        max_attempts=3,
    )

    state = await store.require_state(JOB_ID)
    assert state.status == "queued"
    assert queue.calls == [(JOB_ID, 1)]
