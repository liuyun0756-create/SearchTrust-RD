from __future__ import annotations

import asyncio
from datetime import timedelta
import json
import os
from uuid import UUID

import httpx
import pytest
from arq.connections import ArqRedis
from redis.asyncio import Redis

from app.jobs_v22.checkpoints import JobCheckpoints
from app.jobs_v22.errors import JobLeaseLost
from app.jobs_v22.models import utc_now
from app.jobs_v22.digest import canonical_json_bytes, request_digest
from app.jobs_v22.reconciler import reconcile_once
from app.jobs_v22.store import DurableJobStore
from app.jobs_v22.executor import UnavailableV22Executor
from app.jobs_v22.verified_executor import VerifiedV22Executor, V22ExecutorRouter
from app.jobs_v22.verified_input_resolver import SupabaseVerifiedInputResolver
from app.jobs_v22.verified_models import VerifiedRequestEnvelope
from app.jobs_v22.verified_report_pipeline import VerifiedReportPipeline
from app.jobs_v22.verified_result_persistence import SupabaseVerifiedResultPersister
from app.jobs_v22.worker import execute_v22_job
from test_api_v2_jobs import verified_task_payload
from verified_pipeline_helpers import (
    JOB_ID as VERIFIED_JOB_ID,
    frozen_public_fixture,
    resolve_fixture,
)
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
    request_payload = {
        "schema_version": "v22_verified_request_envelope_v1",
        "verified_request": verified_task_payload(),
    }
    await store.register_job(
        job_id=JOB_ID,
        case_id=CASE_ID,
        idempotency_key="restart-probe",
        request_payload=request_payload,
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
    assert checkpoint == {
        "job_id": str(JOB_ID),
        "created_by_generation": 1,
        "request_digest": request_digest(
            VerifiedRequestEnvelope.model_validate_json(canonical_json_bytes(request_payload))
        ),
    }

    synced_generations: list[int] = []

    class StoreSynchronizer:
        async def sync(self, job_id: UUID) -> bool:
            state = await store.require_state(job_id)
            synced_generations.append(state.run_generation)
            await store.mark_callback_synced(job_id, state.revision)
            return True

    await reconcile_once(
        store=store,
        queue=queue,
        synchronizer=StoreSynchronizer(),
        now=utc_now() + timedelta(minutes=4),
        stale_seconds=180,
        max_attempts=3,
    )
    recovered = await store.require_state(JOB_ID)
    assert recovered.status == "queued"
    assert recovered.run_generation == 2
    assert synced_generations == [1, 2]

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
    assert completed.report.report_version.report_type == "verified_execution"
    assert completed.report.report_version.report_id == JOB_ID
    assert await store.get_request(JOB_ID) == request_payload
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


async def test_real_redis_takeover_replays_one_atomically_settled_verified_attempt(
    redis_client: Redis,
    redis_prefix: str,
) -> None:
    """Exercise the dangerous restart path with the production Verified chain."""

    frozen_payload, _, _, _ = frozen_public_fixture()
    _, verified_request = await resolve_fixture()
    request_payload = VerifiedRequestEnvelope(
        schema_version="v22_verified_request_envelope_v1",
        verified_request=verified_request,
    ).model_dump(mode="json")
    database = {
        "status": "queued",
        "report_id": None,
        "charge_state": "reserved",
        "debit_count": 1,
        "stored_generation": 1,
        "report_payload": None,
    }
    resolver_generations: list[int] = []
    persister_generations: list[int] = []

    async def database_rpc(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if request.url.path.endswith("/resolve_v22_verified_analysis_input"):
            generation = body["p_run_generation"]
            resolver_generations.append(generation)
            active = (
                database["status"] in {"queued", "running"}
                and generation == database["stored_generation"]
            )
            replay = (
                database["status"] == "succeeded"
                and database["report_id"] == str(VERIFIED_JOB_ID)
                and database["charge_state"] == "consumed"
                and database["report_payload"] is not None
                and generation >= database["stored_generation"]
            )
            return httpx.Response(
                200 if active or replay else 409,
                json=frozen_payload if active or replay else {"code": "invalid"},
                request=request,
            )
        if request.url.path.endswith("/persist_v22_verified_result"):
            generation = body["p_run_generation"]
            persister_generations.append(generation)
            if database["status"] in {"queued", "running"}:
                assert generation == database["stored_generation"] == 1
                database.update(
                    status="succeeded",
                    report_id=str(VERIFIED_JOB_ID),
                    charge_state="consumed",
                    report_payload=body["p_report_payload"],
                )
                raise asyncio.CancelledError
            assert generation == 2 > database["stored_generation"]
            assert body["p_report_payload"] == database["report_payload"]
            return httpx.Response(
                200,
                json=[{"report_id": str(VERIFIED_JOB_ID), "idempotent": True}],
                request=request,
            )
        raise AssertionError(f"unexpected RPC path: {request.url.path}")

    store = DurableJobStore(
        redis_client,
        prefix=redis_prefix,
        state_ttl_seconds=120,
        job_timeout_seconds=1200,
    )
    started = utc_now()
    await store.register_job(
        job_id=VERIFIED_JOB_ID,
        case_id=CASE_ID,
        idempotency_key="verified-atomic-commit-loss",
        request_payload=request_payload,
        now=started,
    )

    class DatabaseCallback:
        async def sync(self, job_id):
            state = await store.require_state(job_id)
            if database["status"] != "succeeded":
                database["status"] = state.status
            return True

    async with httpx.AsyncClient(transport=httpx.MockTransport(database_rpc)) as client:
        ctx = {
            "store": store,
            "executor": V22ExecutorRouter(
                prospect_executor=UnavailableV22Executor(),
                verified_executor=VerifiedV22Executor(
                    resolver=SupabaseVerifiedInputResolver(
                        url="https://project.supabase.co",
                        service_role_key="fake-service",
                        http_client=client,
                    ),
                    pipeline=VerifiedReportPipeline(clock=lambda: started),
                    persister=SupabaseVerifiedResultPersister(
                        url="https://project.supabase.co",
                        service_role_key="fake-service",
                        http_client=client,
                    ),
                ),
            ),
            "callback_synchronizer": DatabaseCallback(),
            "max_attempts": 3,
            "state_ttl_seconds": 120,
            "redis": redis_client,
        }
        with pytest.raises(asyncio.CancelledError):
            await execute_v22_job(ctx, str(VERIFIED_JOB_ID), 1)
        abandoned = await store.require_state(VERIFIED_JOB_ID)
        assert abandoned.status == "running"
        assert abandoned.run_generation == 1

        takeover = await store.take_over_stale(
            VERIFIED_JOB_ID,
            expected_generation=1,
            now=started + timedelta(minutes=4),
        )
        assert takeover.applied is True
        assert takeover.state.run_generation == 2
        await execute_v22_job(ctx, str(VERIFIED_JOB_ID), 2)

    completed = await store.require_state(VERIFIED_JOB_ID)
    assert completed.status == "succeeded"
    assert completed.run_generation == 2
    assert completed.attempt_count == 2
    assert completed.report is not None
    assert completed.report.report_version.report_id == VERIFIED_JOB_ID
    assert resolver_generations == [1, 2]
    assert persister_generations == [1, 2]
    assert database["stored_generation"] == 1
    assert database["debit_count"] == 1
    assert database["charge_state"] == "consumed"
