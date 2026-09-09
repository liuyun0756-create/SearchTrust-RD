from datetime import datetime, timedelta, timezone
from uuid import UUID

import fakeredis.aioredis
import pytest

from app.jobs_v22.reconciler import reconcile_once
from app.jobs_v22.store import DurableJobStore


JOB_ID = UUID("55555555-5555-4555-8555-555555555555")
CASE_ID = UUID("11111111-1111-4111-8111-111111111111")
NOW = datetime(2026, 8, 27, 8, 0, tzinfo=timezone.utc)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class RecordingQueue:
    def __init__(self) -> None:
        self.calls: list[tuple[UUID, int]] = []

    async def enqueue(self, job_id: UUID, generation: int) -> bool:
        call = (job_id, generation)
        if call in self.calls:
            return False
        self.calls.append(call)
        return True


class RecordingSynchronizer:
    def __init__(self) -> None:
        self.calls: list[UUID] = []

    async def sync(self, job_id: UUID) -> bool:
        self.calls.append(job_id)
        return True


async def build_store() -> DurableJobStore:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    store = DurableJobStore(redis, prefix="test:v22", state_ttl_seconds=604800)
    await store.register_job(
        job_id=JOB_ID,
        case_id=CASE_ID,
        idempotency_key="intent-1",
        request_payload={"case_id": str(CASE_ID)},
        now=NOW,
    )
    return store


@pytest.mark.anyio
async def test_reconciler_requeues_stale_queued_orphan_once() -> None:
    store = await build_store()
    queue = RecordingQueue()
    synchronizer = RecordingSynchronizer()

    await reconcile_once(
        store=store,
        queue=queue,
        synchronizer=synchronizer,
        now=NOW + timedelta(minutes=10),
        stale_seconds=180,
        max_attempts=3,
    )
    await reconcile_once(
        store=store,
        queue=queue,
        synchronizer=synchronizer,
        now=NOW + timedelta(minutes=10),
        stale_seconds=180,
        max_attempts=3,
    )

    assert queue.calls == [(JOB_ID, 2)]
    assert JOB_ID in synchronizer.calls


@pytest.mark.anyio
async def test_reconciler_closes_stale_running_job_at_attempt_limit() -> None:
    store = await build_store()
    queue = RecordingQueue()
    await store.transition(
        JOB_ID,
        status="running",
        stage="collecting_site",
        progress=15,
        message="Running",
        now=NOW + timedelta(seconds=1),
        attempt_count=3,
    )

    await reconcile_once(
        store=store,
        queue=queue,
        synchronizer=None,
        now=NOW + timedelta(minutes=10),
        stale_seconds=180,
        max_attempts=3,
    )
    state = await store.require_state(JOB_ID)

    assert state.status == "failed"
    assert state.error is not None
    assert state.error.error_code == "JOB_RETRY_EXHAUSTED"
    assert queue.calls == []


@pytest.mark.anyio
async def test_reconciler_requeues_stale_running_job_below_attempt_limit() -> None:
    store = await build_store()
    queue = RecordingQueue()
    await store.transition(
        JOB_ID,
        status="running",
        stage="collecting_site",
        progress=15,
        message="Running",
        now=NOW + timedelta(seconds=1),
        attempt_count=1,
    )

    await reconcile_once(
        store=store,
        queue=queue,
        synchronizer=None,
        now=NOW + timedelta(minutes=10),
        stale_seconds=180,
        max_attempts=3,
    )
    state = await store.require_state(JOB_ID)

    assert state.status == "queued"
    assert queue.calls == [(JOB_ID, 2)]
    assert state.run_generation == 2


@pytest.mark.anyio
async def test_reconciler_closes_job_at_immutable_deadline() -> None:
    store = await build_store()
    queue = RecordingQueue()

    await reconcile_once(
        store=store,
        queue=queue,
        synchronizer=None,
        now=NOW + timedelta(minutes=21),
        stale_seconds=180,
        max_attempts=3,
    )
    state = await store.require_state(JOB_ID)

    assert state.status == "failed"
    assert state.error is not None
    assert state.error.error_code == "JOB_DEADLINE_EXCEEDED"
    assert queue.calls == []
