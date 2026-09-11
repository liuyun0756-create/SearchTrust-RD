from __future__ import annotations

from datetime import timedelta
from uuid import UUID

import pytest

from app.jobs_v22.models import utc_now
from app.jobs_v22.reconciler import reconcile_once
from app.jobs_v22.store import DurableJobStore


pytestmark = [pytest.mark.redis_integration, pytest.mark.anyio]

CASE_ID = UUID("11111111-1111-4111-8111-111111111111")


class RecordingQueue:
    def __init__(self) -> None:
        self.calls: list[tuple[UUID, int]] = []

    async def enqueue(self, job_id: UUID, generation: int) -> bool:
        call = (job_id, generation)
        if call in self.calls:
            return False
        self.calls.append(call)
        return True


async def _register(store: DurableJobStore, job_id: UUID, now) -> None:
    await store.register_job(
        job_id=job_id,
        case_id=CASE_ID,
        idempotency_key=f"reconcile-{job_id}",
        request_payload={"case_id": str(CASE_ID), "job_id": str(job_id)},
        now=now,
    )


async def test_reconciliation_requeues_one_generation_only_once(
    real_store: DurableJobStore,
) -> None:
    job_id = UUID("55555555-5555-4555-8555-555555555553")
    started = utc_now()
    await _register(real_store, job_id, started)
    await real_store.transition(
        job_id,
        status="running",
        stage="collecting_site",
        progress=5,
        message="Interrupted",
        now=started + timedelta(seconds=1),
        attempt_count=1,
    )
    queue = RecordingQueue()
    reconciliation_time = started + timedelta(minutes=4)

    await reconcile_once(
        store=real_store,
        queue=queue,
        synchronizer=None,
        now=reconciliation_time,
        stale_seconds=180,
        max_attempts=3,
    )
    await reconcile_once(
        store=real_store,
        queue=queue,
        synchronizer=None,
        now=reconciliation_time,
        stale_seconds=180,
        max_attempts=3,
    )

    state = await real_store.require_state(job_id)
    assert state.status == "queued"
    assert state.run_generation == 2
    assert queue.calls == [(job_id, 2)]


@pytest.mark.parametrize(
    ("job_id", "deadline_minutes", "attempt_count", "error_code"),
    [
        (UUID("55555555-5555-4555-8555-555555555554"), 21, 0, "JOB_DEADLINE_EXCEEDED"),
        (UUID("55555555-5555-4555-8555-555555555555"), 10, 3, "JOB_RETRY_EXHAUSTED"),
    ],
)
async def test_deadline_and_retry_exhaustion_stay_terminal_after_reconciliation_restart(
    real_store: DurableJobStore,
    job_id: UUID,
    deadline_minutes: int,
    attempt_count: int,
    error_code: str,
) -> None:
    started = utc_now()
    await _register(real_store, job_id, started)
    if attempt_count:
        await real_store.transition(
            job_id,
            status="running",
            stage="collecting_site",
            progress=10,
            message="Interrupted at retry limit",
            now=started + timedelta(seconds=1),
            attempt_count=attempt_count,
        )
    queue = RecordingQueue()
    reconciliation_time = started + timedelta(minutes=deadline_minutes)

    for _ in range(3):
        await reconcile_once(
            store=real_store,
            queue=queue,
            synchronizer=None,
            now=reconciliation_time,
            stale_seconds=180,
            max_attempts=3,
        )
    state = await real_store.require_state(job_id)
    stable_revision = state.revision
    await reconcile_once(
        store=real_store,
        queue=queue,
        synchronizer=None,
        now=reconciliation_time + timedelta(minutes=10),
        stale_seconds=180,
        max_attempts=3,
    )

    assert state.status == "failed"
    assert state.error is not None and state.error.error_code == error_code
    assert (await real_store.require_state(job_id)).revision == stable_revision
    assert queue.calls == []
