import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

import fakeredis.aioredis
import pytest

from app.jobs_v22.errors import IdempotencyConflict, InvalidJobTransition, JobIdentityConflict
from app.jobs_v22.keys import JobRedisKeys
from app.jobs_v22.models import JobErrorState
from app.jobs_v22.store import DurableJobStore


JOB_ID = UUID("55555555-5555-4555-8555-555555555555")
OTHER_JOB_ID = UUID("66666666-6666-4666-8666-666666666666")
CASE_ID = UUID("11111111-1111-4111-8111-111111111111")
OTHER_CASE_ID = UUID("22222222-2222-4222-8222-222222222222")
NOW = datetime(2026, 8, 27, 8, 0, tzinfo=timezone.utc)
REQUEST = {"case_id": str(CASE_ID), "report_type": "prospect", "queries": ["a", "b", "c"]}


@pytest.fixture
def fake_server() -> fakeredis.FakeServer:
    return fakeredis.FakeServer()


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def redis(fake_server: fakeredis.FakeServer) -> fakeredis.aioredis.FakeRedis:
    return fakeredis.aioredis.FakeRedis(server=fake_server, decode_responses=False)


@pytest.fixture
def store(redis: fakeredis.aioredis.FakeRedis) -> DurableJobStore:
    return DurableJobStore(redis, prefix="test:v22", state_ttl_seconds=604800)


async def register(store: DurableJobStore, **overrides: object):
    values: dict[str, object] = {
        "job_id": JOB_ID,
        "case_id": CASE_ID,
        "idempotency_key": "generation-intent-1",
        "request_payload": REQUEST,
        "now": NOW,
    }
    values.update(overrides)
    return await store.register_job(**values)


@pytest.mark.anyio
async def test_concurrent_registration_creates_one_logical_job(store: DurableJobStore) -> None:
    results = await asyncio.gather(*(register(store) for _ in range(12)))

    assert sum(not result.replayed for result in results) == 1
    assert {result.state.job_id for result in results} == {JOB_ID}
    assert await store.active_count() == 1


@pytest.mark.anyio
async def test_same_idempotency_key_with_different_request_conflicts(store: DurableJobStore) -> None:
    await register(store)

    with pytest.raises(IdempotencyConflict):
        await register(store, request_payload={**REQUEST, "queries": ["different", "b", "c"]})


@pytest.mark.anyio
async def test_job_id_cannot_be_rebound_to_another_case(store: DurableJobStore) -> None:
    await register(store)

    with pytest.raises(JobIdentityConflict):
        await register(
            store,
            case_id=OTHER_CASE_ID,
            idempotency_key="another-intent",
            request_payload={**REQUEST, "case_id": str(OTHER_CASE_ID)},
        )


@pytest.mark.anyio
async def test_new_store_instance_recovers_existing_state(
    fake_server: fakeredis.FakeServer,
    store: DurableJobStore,
) -> None:
    created = await register(store)
    replacement_redis = fakeredis.aioredis.FakeRedis(server=fake_server, decode_responses=False)
    replacement = DurableJobStore(replacement_redis, prefix="test:v22", state_ttl_seconds=604800)

    recovered = await replacement.get_state(JOB_ID)

    assert recovered == created.state
    assert await replacement.get_request(JOB_ID) == REQUEST


@pytest.mark.anyio
async def test_revisions_increase_and_first_terminal_state_wins(store: DurableJobStore) -> None:
    await register(store)
    running = await store.transition(
        JOB_ID,
        status="running",
        stage="collecting_site",
        progress=10,
        message="Collecting site",
        now=NOW + timedelta(seconds=1),
        attempt_count=1,
    )
    error = JobErrorState(
        error_code="V22_INPUT_INVALID",
        user_message="The request is invalid.",
        retryable=False,
        stage="failed",
        diagnostic_id=UUID("77777777-7777-4777-8777-777777777777"),
    )
    failed = await store.transition(
        JOB_ID,
        status="failed",
        stage="failed",
        progress=10,
        message=error.user_message,
        now=NOW + timedelta(seconds=2),
        error=error,
    )
    duplicate = await store.transition(
        JOB_ID,
        status="failed",
        stage="failed",
        progress=10,
        message="A different failure",
        now=NOW + timedelta(seconds=3),
        error=error.model_copy(update={"error_code": "SECOND_FAILURE"}),
    )

    assert running.applied is True
    assert running.state.revision == 2
    assert failed.applied is True
    assert failed.state.revision == 3
    assert duplicate.applied is False
    assert duplicate.state.error.error_code == "V22_INPUT_INVALID"
    assert await store.active_count() == 0
    assert await store.pending_callback_count() == 1


@pytest.mark.anyio
async def test_illegal_state_transition_is_rejected(store: DurableJobStore) -> None:
    await register(store)

    with pytest.raises(InvalidJobTransition):
        await store.transition(
            JOB_ID,
            status="succeeded",
            stage="completed",
            progress=100,
            message="Complete",
            now=NOW + timedelta(seconds=1),
        )


@pytest.mark.anyio
async def test_state_and_request_receive_configured_ttl(
    redis: fakeredis.aioredis.FakeRedis,
    store: DurableJobStore,
) -> None:
    await register(store)
    keys = JobRedisKeys("test:v22")

    state_ttl = await redis.ttl(keys.state(JOB_ID))
    request_ttl = await redis.ttl(keys.request(JOB_ID))

    assert 604790 <= state_ttl <= 604800
    assert 604790 <= request_ttl <= 604800


def test_job_state_fixture_is_valid_json() -> None:
    fixture = Path(__file__).parent / "fixtures" / "v22_job_state.json"
    payload = json.loads(fixture.read_text(encoding="utf-8"))

    assert payload["status"] == "queued"
    assert payload["revision"] == 1
