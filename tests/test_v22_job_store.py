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

    with pytest.raises(JobIdentityConflict):
        await register(store, idempotency_key="another-intent")


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


@pytest.mark.anyio
async def test_manual_retry_reuses_logical_job_and_increases_generation(store: DurableJobStore) -> None:
    await register(store)
    await store.transition(
        JOB_ID,
        status="running",
        stage="collecting_site",
        progress=5,
        message="Running",
        now=NOW + timedelta(seconds=1),
        attempt_count=1,
    )
    error = JobErrorState(
        error_code="JOB_RETRY_EXHAUSTED",
        user_message="Please retry later.",
        retryable=True,
        stage="failed",
        diagnostic_id=UUID("77777777-7777-4777-8777-777777777777"),
    )
    await store.transition(
        JOB_ID,
        status="failed",
        stage="failed",
        progress=5,
        message=error.user_message,
        now=NOW + timedelta(seconds=2),
        error=error,
    )

    retried = await store.retry_failed(JOB_ID, now=NOW + timedelta(seconds=3))

    assert retried.job_id == JOB_ID
    assert retried.status == "queued"
    assert retried.run_generation == 2
    assert retried.attempt_count == 1
    assert retried.error is None


@pytest.mark.anyio
async def test_stale_takeover_fences_old_generation(store: DurableJobStore) -> None:
    await register(store)
    takeover = await store.take_over_stale(
        JOB_ID,
        expected_generation=1,
        now=NOW + timedelta(minutes=4),
    )

    assert takeover.applied is True
    assert takeover.state.run_generation == 2
    assert await store.pending_recovery_generation(JOB_ID) == 2
    repeated = await store.take_over_stale(
        JOB_ID,
        expected_generation=2,
        now=NOW + timedelta(minutes=8),
    )
    assert repeated.applied is False
    assert repeated.state.run_generation == 2
    assert await store.discard_pending_recovery(JOB_ID, generation=1) is False
    assert await store.pending_recovery_generation(JOB_ID) == 2
    with pytest.raises(Exception, match="JOB_LEASE_LOST"):
        await store.transition(
            JOB_ID,
            status="running",
            stage="collecting_site",
            progress=10,
            message="Old worker",
            now=NOW + timedelta(minutes=4, seconds=1),
            expected_generation=1,
        )


@pytest.mark.anyio
async def test_missing_state_cleanup_never_removes_a_newer_recovery_marker(
    store: DurableJobStore,
) -> None:
    await register(store)
    await store.take_over_stale(
        JOB_ID,
        expected_generation=1,
        now=NOW + timedelta(minutes=4),
    )
    await store.redis.delete(store.keys.state(JOB_ID))

    assert await store.discard_missing_job_recovery(
        JOB_ID, generation=1
    ) is False
    assert await store.pending_recovery_generation(JOB_ID) == 2
    assert await store.discard_missing_job_recovery(
        JOB_ID, generation=2
    ) is True
    assert await store.pending_recovery_generation(JOB_ID) is None
    assert JOB_ID not in await store.list_stale_jobs(NOW + timedelta(days=1))
    assert JOB_ID not in await store.list_pending_callbacks()


@pytest.mark.anyio
async def test_missing_state_index_cleanup_does_not_remove_a_recreated_job(
    store: DurableJobStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await register(store)
    state_key = store.keys.state(JOB_ID)
    original_state = await store.require_state(JOB_ID)
    recreated_state = original_state.model_copy(
        update={"run_generation": 2, "revision": 2}
    )
    await store.redis.delete(state_key)

    entered = asyncio.Event()
    release = asyncio.Event()
    real_pipeline = store.redis.pipeline

    class PausingPipeline:
        def __init__(self):
            self.delegate = real_pipeline(transaction=True)
            self.paused = False

        async def __aenter__(self):
            await self.delegate.__aenter__()
            return self

        async def __aexit__(self, *args):
            return await self.delegate.__aexit__(*args)

        def __getattr__(self, name: str):
            return getattr(self.delegate, name)

        async def exists(self, key: str):
            result = await self.delegate.exists(key)
            if key == state_key and not self.paused:
                self.paused = True
                entered.set()
                await release.wait()
            return result

    monkeypatch.setattr(
        store.redis,
        "pipeline",
        lambda transaction=True: PausingPipeline(),
    )
    cleanup = asyncio.create_task(store.discard_missing_job_indexes(JOB_ID))
    await entered.wait()
    await store.redis.set(
        state_key,
        recreated_state.model_dump_json(),
        ex=store.state_ttl_seconds,
    )
    await store.redis.zadd(store.keys.active, {str(JOB_ID): NOW.timestamp() + 1})
    await store.redis.zadd(store.keys.sync_pending, {str(JOB_ID): 2})
    await store.redis.zadd(store.keys.recovery_pending, {str(JOB_ID): 2})
    release.set()

    assert await cleanup is False
    assert await store.redis.exists(state_key)
    assert await store.redis.zscore(store.keys.active, str(JOB_ID)) is not None
    assert await store.redis.zscore(store.keys.sync_pending, str(JOB_ID)) == 2
    assert await store.pending_recovery_generation(JOB_ID) == 2


@pytest.mark.anyio
async def test_lease_refresh_and_release_require_same_token(store: DurableJobStore) -> None:
    await register(store)
    assert await store.acquire_lease(JOB_ID, generation=1, token="owner", ttl_seconds=180)
    assert not await store.refresh_lease(JOB_ID, generation=1, token="other", ttl_seconds=180)
    assert await store.refresh_lease(JOB_ID, generation=1, token="owner", ttl_seconds=180)
    assert not await store.release_lease(JOB_ID, generation=1, token="other")
    assert await store.release_lease(JOB_ID, generation=1, token="owner")


def test_job_state_fixture_is_valid_json() -> None:
    fixture = Path(__file__).parent / "fixtures" / "v22_job_state.json"
    payload = json.loads(fixture.read_text(encoding="utf-8"))

    assert payload["status"] == "queued"
    assert payload["revision"] == 1
