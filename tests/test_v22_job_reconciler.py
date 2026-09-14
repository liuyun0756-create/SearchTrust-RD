import asyncio
from datetime import datetime, timedelta, timezone
from uuid import UUID

import fakeredis.aioredis
import pytest

from app.jobs_v22.reconciler import reconcile_once
from app.jobs_v22.models import JobErrorState
from app.jobs_v22.store import DurableJobStore


JOB_ID = UUID("55555555-5555-4555-8555-555555555555")
SECOND_JOB_ID = UUID("66666666-6666-4666-8666-666666666666")
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


class ControlledSynchronizer:
    def __init__(
        self,
        store: DurableJobStore,
        outcomes: list[bool | BaseException],
    ) -> None:
        self.store = store
        self.outcomes = outcomes
        self.calls: list[tuple[UUID, int]] = []
        self.statuses: list[str] = []

    async def sync(self, job_id: UUID) -> bool:
        state = await self.store.require_state(job_id)
        self.calls.append((job_id, state.run_generation))
        self.statuses.append(state.status)
        outcome = self.outcomes[min(len(self.calls) - 1, len(self.outcomes) - 1)]
        if isinstance(outcome, BaseException):
            raise outcome
        if outcome:
            await self.store.mark_callback_synced(job_id, state.revision)
        return outcome


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
    synchronizer = RecordingSynchronizer()
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
        synchronizer=synchronizer,
        now=NOW + timedelta(minutes=10),
        stale_seconds=180,
        max_attempts=3,
    )
    state = await store.require_state(JOB_ID)

    assert state.status == "queued"
    assert queue.calls == [(JOB_ID, 2)]
    assert state.run_generation == 2


@pytest.mark.anyio
async def test_recovery_never_enqueues_until_generation_callback_finishes() -> None:
    store = await build_store()
    await store.mark_callback_synced(JOB_ID, 1)
    await store.transition(
        JOB_ID,
        status="running",
        stage="collecting_site",
        progress=15,
        message="Interrupted",
        now=NOW + timedelta(seconds=1),
        attempt_count=1,
    )
    await store.mark_callback_synced(JOB_ID, 2)
    entered = asyncio.Event()
    release = asyncio.Event()
    database_generation = 1

    class GenerationGuardedQueue(RecordingQueue):
        async def enqueue(self, job_id: UUID, generation: int) -> bool:
            # A physical Worker (and therefore its resolver) cannot observe the
            # new generation until the durable database owner matches it.
            assert database_generation == generation
            return await super().enqueue(job_id, generation)

    queue = GenerationGuardedQueue()

    class BarrierSynchronizer:
        async def sync(self, job_id: UUID) -> bool:
            nonlocal database_generation
            state = await store.require_state(job_id)
            assert state.run_generation == 2
            entered.set()
            await release.wait()
            database_generation = state.run_generation
            await store.mark_callback_synced(job_id, state.revision)
            return True

    task = asyncio.create_task(reconcile_once(
        store=store,
        queue=queue,
        synchronizer=BarrierSynchronizer(),
        now=NOW + timedelta(minutes=10),
        stale_seconds=180,
        max_attempts=3,
    ))
    await entered.wait()
    assert queue.calls == []
    release.set()
    await task
    assert queue.calls == [(JOB_ID, 2)]


@pytest.mark.anyio
@pytest.mark.parametrize("failure", [False, TimeoutError()])
async def test_failed_recovery_sync_retries_same_generation_then_enqueues_once(
    failure: bool | BaseException,
) -> None:
    store = await build_store()
    await store.mark_callback_synced(JOB_ID, 1)
    queue = RecordingQueue()
    synchronizer = ControlledSynchronizer(store, [failure, True])

    await reconcile_once(
        store=store, queue=queue, synchronizer=synchronizer,
        now=NOW + timedelta(minutes=10), stale_seconds=180, max_attempts=3,
    )
    assert (await store.require_state(JOB_ID)).run_generation == 2
    assert queue.calls == []

    await reconcile_once(
        store=store, queue=queue, synchronizer=synchronizer,
        now=NOW + timedelta(minutes=14), stale_seconds=180, max_attempts=3,
    )
    await reconcile_once(
        store=store, queue=queue, synchronizer=synchronizer,
        now=NOW + timedelta(minutes=14, seconds=1), stale_seconds=180, max_attempts=3,
    )
    assert (await store.require_state(JOB_ID)).run_generation == 2
    assert queue.calls == [(JOB_ID, 2)]
    assert synchronizer.calls == [(JOB_ID, 2), (JOB_ID, 2)]


@pytest.mark.anyio
async def test_recovery_resumes_same_generation_if_process_dies_after_db_sync() -> None:
    store = await build_store()
    await store.mark_callback_synced(JOB_ID, 1)
    queue = RecordingQueue()
    database_generation = 1

    class CrashAfterDbSync:
        calls = 0

        async def sync(self, job_id: UUID) -> bool:
            nonlocal database_generation
            state = await store.require_state(job_id)
            self.calls += 1
            database_generation = state.run_generation
            if self.calls == 1:
                # The database accepted gen2, but the reconciler process exited
                # before it could enqueue the corresponding physical job.
                raise asyncio.CancelledError
            await store.mark_callback_synced(job_id, state.revision)
            return True

    synchronizer = CrashAfterDbSync()

    with pytest.raises(asyncio.CancelledError):
        await reconcile_once(
            store=store, queue=queue, synchronizer=synchronizer,
            now=NOW + timedelta(minutes=10), stale_seconds=180, max_attempts=3,
        )
    assert (await store.require_state(JOB_ID)).run_generation == 2
    assert database_generation == 2
    assert queue.calls == []

    await reconcile_once(
        store=store, queue=queue, synchronizer=synchronizer,
        now=NOW + timedelta(minutes=10, seconds=1), stale_seconds=180, max_attempts=3,
    )
    assert (await store.require_state(JOB_ID)).run_generation == 2
    assert queue.calls == [(JOB_ID, 2)]


@pytest.mark.anyio
async def test_recovery_queue_dedup_closes_enqueue_then_crash_window() -> None:
    store = await build_store()
    await store.mark_callback_synced(JOB_ID, 1)
    synchronizer = ControlledSynchronizer(store, [True])

    class CrashAfterEnqueueQueue(RecordingQueue):
        async def enqueue(self, job_id: UUID, generation: int) -> bool:
            created = await super().enqueue(job_id, generation)
            if created:
                raise asyncio.CancelledError
            return created

    queue = CrashAfterEnqueueQueue()
    with pytest.raises(asyncio.CancelledError):
        await reconcile_once(
            store=store, queue=queue, synchronizer=synchronizer,
            now=NOW + timedelta(minutes=10), stale_seconds=180, max_attempts=3,
        )
    await reconcile_once(
        store=store, queue=queue, synchronizer=synchronizer,
        now=NOW + timedelta(minutes=10, seconds=1), stale_seconds=180, max_attempts=3,
    )
    assert (await store.require_state(JOB_ID)).run_generation == 2
    assert queue.calls == [(JOB_ID, 2)]


@pytest.mark.anyio
async def test_missing_recovery_state_is_removed_without_blocking_the_next_job() -> None:
    store = await build_store()
    await store.take_over_stale(
        JOB_ID, expected_generation=1, now=NOW + timedelta(minutes=4)
    )
    await store.register_job(
        job_id=SECOND_JOB_ID,
        case_id=CASE_ID,
        idempotency_key="intent-2",
        request_payload={"case_id": str(CASE_ID), "sequence": 2},
        now=NOW,
    )
    await store.mark_callback_synced(SECOND_JOB_ID, 1)
    await store.take_over_stale(
        SECOND_JOB_ID, expected_generation=1, now=NOW + timedelta(minutes=4)
    )
    await store.redis.delete(store.keys.state(JOB_ID))
    queue = RecordingQueue()
    synchronizer = ControlledSynchronizer(store, [True])

    await reconcile_once(
        store=store, queue=queue, synchronizer=synchronizer,
        now=NOW + timedelta(minutes=4, seconds=1), stale_seconds=180, max_attempts=3,
    )

    assert await store.pending_recovery_generation(JOB_ID) is None
    assert queue.calls == [(SECOND_JOB_ID, 2)]
    assert await store.list_pending_recoveries() == []


@pytest.mark.anyio
async def test_orphaned_active_page_does_not_starve_the_next_valid_job() -> None:
    store = await build_store()
    await store.mark_callback_synced(JOB_ID, 1)
    orphan_ids = [UUID(int=index + 1000) for index in range(100)]
    await store.redis.zadd(
        store.keys.active,
        {str(job_id): NOW.timestamp() - 1 for job_id in orphan_ids},
    )
    queue = RecordingQueue()
    synchronizer = ControlledSynchronizer(store, [True])

    await reconcile_once(
        store=store,
        queue=queue,
        synchronizer=synchronizer,
        now=NOW + timedelta(minutes=10),
        stale_seconds=180,
        max_attempts=3,
    )
    assert queue.calls == []

    await reconcile_once(
        store=store,
        queue=queue,
        synchronizer=synchronizer,
        now=NOW + timedelta(minutes=10, seconds=1),
        stale_seconds=180,
        max_attempts=3,
    )
    assert queue.calls == [(JOB_ID, 2)]
    assert await store.active_count() == 1


@pytest.mark.anyio
async def test_orphaned_callback_page_does_not_starve_the_next_valid_job() -> None:
    store = await build_store()
    orphan_ids = [UUID(int=index + 1000) for index in range(100)]
    await store.redis.zadd(
        store.keys.sync_pending,
        {str(job_id): 0 for job_id in orphan_ids},
    )
    synchronizer = ControlledSynchronizer(store, [True])

    await reconcile_once(
        store=store,
        queue=RecordingQueue(),
        synchronizer=synchronizer,
        now=NOW + timedelta(seconds=1),
        stale_seconds=180,
        max_attempts=3,
    )
    assert synchronizer.calls == []

    await reconcile_once(
        store=store,
        queue=RecordingQueue(),
        synchronizer=synchronizer,
        now=NOW + timedelta(seconds=2),
        stale_seconds=180,
        max_attempts=3,
    )
    assert synchronizer.calls == [(JOB_ID, 1)]
    assert await store.pending_callback_count() == 0


@pytest.mark.anyio
async def test_corrupt_active_and_callback_members_do_not_abort_reconciliation() -> None:
    store = await build_store()
    await store.redis.zadd(
        store.keys.active,
        {
            "not-a-job-id": NOW.timestamp() - 1000,
            "00000000000000000000000000000000": NOW.timestamp() - 1000,
            b"\xff": NOW.timestamp() - 1000,
        },
    )
    await store.redis.zadd(
        store.keys.sync_pending,
        {
            "not-a-job-id": 0,
            "00000000000000000000000000000000": 0,
            b"\xff": 0,
        },
    )
    synchronizer = ControlledSynchronizer(store, [True])

    await reconcile_once(
        store=store,
        queue=RecordingQueue(),
        synchronizer=synchronizer,
        now=NOW + timedelta(seconds=1),
        stale_seconds=180,
        max_attempts=3,
    )

    assert synchronizer.calls == [(JOB_ID, 1)]
    assert await store.redis.zscore(store.keys.active, "not-a-job-id") is None
    assert (
        await store.redis.zscore(
            store.keys.active, "00000000000000000000000000000000"
        )
        is None
    )
    assert await store.redis.zscore(store.keys.sync_pending, "not-a-job-id") is None
    assert (
        await store.redis.zscore(
            store.keys.sync_pending, "00000000000000000000000000000000"
        )
        is None
    )
    assert await store.redis.zscore(store.keys.active, b"\xff") is None
    assert await store.redis.zscore(store.keys.sync_pending, b"\xff") is None


@pytest.mark.anyio
async def test_terminal_and_generation_mismatch_recovery_markers_are_cleaned() -> None:
    store = await build_store()
    await store.redis.zadd(store.keys.recovery_pending, {str(JOB_ID): 2})
    queue = RecordingQueue()
    synchronizer = RecordingSynchronizer()

    await reconcile_once(
        store=store, queue=queue, synchronizer=synchronizer,
        now=NOW + timedelta(seconds=1), stale_seconds=180, max_attempts=3,
    )
    assert await store.list_pending_recoveries() == []
    assert (await store.require_state(JOB_ID)).run_generation == 1
    assert queue.calls == []

    error = JobErrorState(
        error_code="JOB_RETRY_EXHAUSTED",
        user_message="Failed",
        retryable=True,
        stage="failed",
        diagnostic_id=UUID("77777777-7777-4777-8777-777777777777"),
    )
    await store.transition(
        JOB_ID, status="failed", stage="failed", progress=0, message="Failed",
        now=NOW + timedelta(seconds=2), error=error,
    )
    await store.redis.zadd(store.keys.recovery_pending, {str(JOB_ID): 1})
    await reconcile_once(
        store=store, queue=queue, synchronizer=synchronizer,
        now=NOW + timedelta(seconds=3), stale_seconds=180, max_attempts=3,
    )
    assert await store.list_pending_recoveries() == []
    assert queue.calls == []


@pytest.mark.anyio
async def test_corrupt_and_over_limit_pending_members_are_bounded_and_do_not_block() -> None:
    store = await build_store()
    await store.redis.delete(store.keys.recovery_pending)
    members = {
        **{str(UUID(int=index + 1000)): 2 for index in range(101)},
        "not-a-job-id": 2,
        "00000000000000000000000000000000": 2,
        str(UUID(int=999)): 0,
    }
    await store.redis.zadd(store.keys.recovery_pending, members)

    await reconcile_once(
        store=store, queue=RecordingQueue(), synchronizer=RecordingSynchronizer(),
        now=NOW, stale_seconds=180, max_attempts=3,
    )
    assert 1 <= await store.redis.zcard(store.keys.recovery_pending) <= 5
    await reconcile_once(
        store=store, queue=RecordingQueue(), synchronizer=RecordingSynchronizer(),
        now=NOW + timedelta(seconds=1), stale_seconds=180, max_attempts=3,
    )
    assert await store.redis.zcard(store.keys.recovery_pending) == 0


@pytest.mark.anyio
async def test_pending_recovery_crossing_deadline_fails_and_uses_callback_path() -> None:
    store = await build_store()
    await store.mark_callback_synced(JOB_ID, 1)
    queue = RecordingQueue()
    synchronizer = ControlledSynchronizer(store, [False, False])

    await reconcile_once(
        store=store, queue=queue, synchronizer=synchronizer,
        now=NOW + timedelta(minutes=10), stale_seconds=180, max_attempts=3,
    )
    assert await store.pending_recovery_generation(JOB_ID) == 2

    await reconcile_once(
        store=store, queue=queue, synchronizer=synchronizer,
        now=NOW + timedelta(minutes=21), stale_seconds=180, max_attempts=3,
    )
    state = await store.require_state(JOB_ID)
    assert state.status == "failed"
    assert state.error is not None and state.error.error_code == "JOB_DEADLINE_EXCEEDED"
    assert await store.pending_recovery_generation(JOB_ID) is None
    assert queue.calls == []
    assert synchronizer.statuses == ["queued", "failed"]


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
