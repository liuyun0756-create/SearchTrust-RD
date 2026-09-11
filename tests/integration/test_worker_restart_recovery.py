from __future__ import annotations

from datetime import timedelta
import os
from uuid import UUID

import pytest
from arq.connections import ArqRedis
from redis.asyncio import Redis

from app.jobs_v22.checkpoints import JobCheckpoints
from app.jobs_v22.errors import JobLeaseLost
from app.jobs_v22.models import utc_now
from app.jobs_v22.reconciler import reconcile_once
from app.jobs_v22.store import DurableJobStore
from tests.integration.support.worker_probe import (
    CHECKPOINT_NAME,
    ProbeQueue,
    ProbeWorkerProcess,
    checkpoint_signal_key,
    resumed_signal_key,
    wait_for_signal,
)


pytestmark = [pytest.mark.redis_integration, pytest.mark.anyio]

JOB_ID = UUID("55555555-5555-4555-8555-555555555551")
CASE_ID = UUID("11111111-1111-4111-8111-111111111111")


async def test_worker_interruption_retains_checkpoint_and_recovers_higher_generation(
    redis_client: Redis,
    arq_pool: ArqRedis,
    redis_test_environment: tuple[str, str],
) -> None:
    _, run_prefix = redis_test_environment
    store = DurableJobStore(
        redis_client,
        prefix=run_prefix,
        state_ttl_seconds=120,
        job_timeout_seconds=1200,
    )
    started = utc_now()
    await store.register_job(
        job_id=JOB_ID,
        case_id=CASE_ID,
        idempotency_key="restart-probe",
        request_payload={"case_id": str(CASE_ID), "report_type": "prospect"},
        now=started,
    )
    queue = ProbeQueue(arq_pool, prefix=run_prefix)
    assert await queue.enqueue(JOB_ID, 1) is True
    assert await queue.enqueue(JOB_ID, 1) is False

    with ProbeWorkerProcess(os.environ) as first_worker:
        await wait_for_signal(
            redis_client,
            checkpoint_signal_key(run_prefix, JOB_ID, 1),
            first_worker,
        )
    assert first_worker.process is not None and first_worker.process.poll() is not None

    interrupted = await store.require_state(JOB_ID)
    checkpoint = await JobCheckpoints(
        redis_client,
        prefix=store.keys.prefix,
        ttl_seconds=120,
    ).get(JOB_ID, CHECKPOINT_NAME)
    assert interrupted.status == "running"
    assert interrupted.run_generation == 1
    assert checkpoint == {"job_id": str(JOB_ID), "created_by_generation": 1}

    await reconcile_once(
        store=store,
        queue=queue,
        synchronizer=None,
        now=utc_now() + timedelta(minutes=4),
        stale_seconds=180,
        max_attempts=3,
    )
    recovered = await store.require_state(JOB_ID)
    assert recovered.status == "queued"
    assert recovered.run_generation == 2

    with ProbeWorkerProcess(os.environ) as replacement_worker:
        await wait_for_signal(
            redis_client,
            resumed_signal_key(run_prefix, JOB_ID, 2),
            replacement_worker,
        )
    assert replacement_worker.process is not None
    assert replacement_worker.process.poll() is not None

    completed = await store.require_state(JOB_ID)
    assert completed.status == "succeeded"
    assert completed.run_generation == 2
    assert completed.attempt_count == 2
    assert completed.report is not None
    before_stale_write = completed.model_copy(deep=True)
    with pytest.raises(JobLeaseLost):
        await store.transition(
            JOB_ID,
            status="running",
            stage="collecting_site",
            progress=10,
            message="Stale owner write",
            now=utc_now(),
            expected_generation=1,
        )
    assert await store.require_state(JOB_ID) == before_stale_write
