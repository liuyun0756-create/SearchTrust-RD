"""Runtime routes and recoverable SSE transport for SearchTrust API v2."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import httpx
from collections.abc import AsyncGenerator
from datetime import datetime, timezone
from typing import Annotated, Any
from uuid import UUID

from arq import create_pool
from arq.connections import RedisSettings
from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from fastapi.responses import StreamingResponse
from pydantic import SecretStr
from redis.exceptions import RedisError

from app.api.v2.dependencies import (
    get_v22_runtime,
    parse_analyze_request,
    parse_verified_task_request,
    require_internal_auth,
    require_v22_analyze_enabled,
    require_v22_verified_analysis_enabled,
    require_v22_capability_enabled,
    parse_capability_request,
    parse_acquisition_request,
    require_v22_acquisition_enabled,
)
from app.api.v2.models import (
    AnalyzeRequest,
    JobError,
    RetryTaskResponse,
    TaskCreateResponse,
    TaskStatusResponse,
    VerifiedTaskRequest,
)
from app.core.config import settings, v22_capability_execution_ready
from app.competitors_v22.market_store import SharedMarketSnapshotStore
from app.competitors_v22.selection import (
    AnalysisRequestEnvelope,
    DiscoverySelectionError,
    DiscoveryVerifier,
    RedisDiscoveryVerifier,
)
from app.competitors_v22.store import CompetitorDiscoveryStore
from app.jobs_v22.errors import (
    DurableJobError,
    IdempotencyConflict,
    JobIdentityConflict,
    JobNewAttemptRequired,
    JobNotFound,
    JobNotRetryable,
    JobDeadlineExceeded,
    TransientJobError,
)
from app.jobs_v22.models import JobState
from app.jobs_v22.capability_runtime_models import CapabilityTaskStatusResponse, CapabilityRequestEnvelope
from app.jobs_v22.capability_input_repository import SupabaseCapabilityInputRepository
from app.jobs_v22.capability_dispatch import authorize_dispatch
from app.report_v22.capability import CapabilityReport
from app.jobs_v22.digest import verified_request_digest
from app.jobs_v22.queue import ArqJobQueue, JobQueue
from app.jobs_v22.queue import ArqAcquisitionPreparationQueue
from app.jobs_v22.capability_acquisition_intake import submit_acquisition
from app.jobs_v22.store import DurableJobStore
from app.jobs_v22.verified_models import VerifiedRequestEnvelope


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v2", tags=["SearchTrust v2 durable tasks"])
SSE_HEARTBEAT = object()


def _secret_value(value: SecretStr | str) -> str:
    return value.get_secret_value() if isinstance(value, SecretStr) else value


class V22JobRuntime:
    def __init__(
        self,
        *,
        store: DurableJobStore,
        queue: JobQueue,
        redis: Any,
        discovery_verifier: DiscoveryVerifier | None = None,
        capability_repository: SupabaseCapabilityInputRepository | None = None,
        capability_http_client: httpx.AsyncClient | None = None,
        acquisition_queue: JobQueue | None = None,
    ) -> None:
        self.store = store
        self.queue = queue
        self.redis = redis
        self.discovery_verifier = discovery_verifier
        self.capability_repository = capability_repository
        self.capability_http_client = capability_http_client
        self.acquisition_queue = acquisition_queue

    async def submit_capability(self, *, user_id: UUID, job_id: UUID,
                                idempotency_key: str, request: CapabilityRequestEnvelope):
        if self.capability_repository is None:
            raise TransientJobError("V22_CAPABILITY_NOT_READY", "This analysis workflow is not available yet.")
        allowed = await authorize_dispatch(
            self.capability_repository, user_id=user_id, case_id=request.capability_request.case_id,
            job_id=job_id, idempotency_key=idempotency_key,
        )
        if allowed.request != request:
            raise JobIdentityConflict()
        # SQL success wins, including Redis loss and sources that have since expired.
        if allowed.completion is not None:
            return allowed.completion
        now = datetime.now(timezone.utc)
        if allowed.deadline_at <= now:
            raise JobDeadlineExceeded()
        if allowed.attempt_count >= settings.V22_JOB_MAX_ATTEMPTS:
            raise TransientJobError("CAPABILITY_RECOVERY_REQUIRED", "This analysis is awaiting recovery.")
        if allowed.cost_counters and not await self.redis.exists(self.store.keys.cost_ledger(job_id)):
            # Aggregate SQL counters cannot reconstruct per-operation claims.
            # Do not recreate an empty cost ledger after partial Redis loss.
            raise TransientJobError("CAPABILITY_RECOVERY_REQUIRED", "This analysis is awaiting recovery.")
        registered = await self.store.register_job(
            job_id=job_id, case_id=allowed.case_id, idempotency_key=idempotency_key,
            request_payload=request.model_dump(mode="json"), now=now,
            created_at=allowed.created_at, deadline_at=allowed.deadline_at,
            initial_generation=allowed.run_generation,
            initial_revision=allowed.revision, initial_attempt_count=allowed.attempt_count,
            initial_cost_counters=allowed.cost_counters,
        )
        state = registered.state
        if (state.run_generation != allowed.run_generation or state.created_at != allowed.created_at
                or state.deadline_at != allowed.deadline_at):
            raise JobIdentityConflict()
        if state.status == "failed":
            raise JobNewAttemptRequired()
        # Re-dispatch a queued replay too: registration may have committed before
        # enqueue failed. ARQ deduplicates the identical job+generation.
        if state.status == "queued":
            await self.queue.enqueue(job_id, state.run_generation)
        return state_to_public(state)

    async def submit(
        self,
        *,
        job_id: UUID,
        idempotency_key: str,
        request: AnalyzeRequest,
        discovery_id: UUID | None,
    ) -> TaskCreateResponse:
        if self.discovery_verifier is None:
            raise DiscoverySelectionError(
                "COMPETITOR_DISCOVERY_UNAVAILABLE",
                "Competitor discovery validation is unavailable.",
            )
        now = datetime.now(timezone.utc)
        discovery_link = await self.discovery_verifier.verify(
            discovery_id=discovery_id,
            request=request,
            now=now,
        )
        envelope = AnalysisRequestEnvelope(
            schema_version="v22_analysis_request_envelope_v1",
            analyze_request=request,
            competitor_discovery=discovery_link,
        )
        registered = await self.store.register_job(
            job_id=job_id,
            case_id=request.case_id,
            idempotency_key=idempotency_key,
            request_payload=envelope.model_dump(mode="json"),
            now=now,
        )
        if not registered.replayed:
            await self.queue.enqueue(job_id, registered.state.run_generation)
        return TaskCreateResponse(job_id=job_id, status="queued", estimated_seconds=600)

    async def submit_verified(
        self,
        *,
        job_id: UUID,
        idempotency_key: str,
        request: VerifiedTaskRequest,
    ) -> TaskCreateResponse:
        envelope = VerifiedRequestEnvelope(
            schema_version="v22_verified_request_envelope_v1",
            verified_request=request,
        )
        registered = await self.store.register_job(
            job_id=job_id,
            case_id=request.case_id,
            idempotency_key=idempotency_key,
            request_payload=envelope.model_dump(mode="json"),
            now=datetime.now(timezone.utc),
        )
        if not registered.replayed:
            await self.queue.enqueue(job_id, registered.state.run_generation)
        return TaskCreateResponse(job_id=job_id, status="queued", estimated_seconds=600)

    async def retry(self, job_id: UUID) -> RetryTaskResponse:
        await self.store.require_state(job_id)
        raise JobNewAttemptRequired()


def state_to_public(state: JobState) -> TaskStatusResponse | CapabilityTaskStatusResponse:
    if isinstance(state.report, CapabilityReport):
        report=state.report
        return CapabilityTaskStatusResponse(schema_version="v22_capability_task_status_v1",job_id=state.job_id,
            revision=state.revision,run_generation=state.run_generation,status="succeeded",stage="completed",progress=100,message=state.message,
            report_reference=dict(report_id=report.id,case_id=state.case_id,schema_version="2.2.2",report_type=report.report_type,
                payload_checksum=verified_request_digest(report.model_dump(mode="json"))),
            created_at=state.created_at,deadline_at=state.deadline_at,updated_at=state.updated_at)
    error = None
    if state.error is not None:
        error = JobError.model_validate(state.error.model_dump())
    return TaskStatusResponse(
        job_id=state.job_id,
        revision=state.revision,
        run_generation=state.run_generation,
        status=state.status,
        stage=state.stage,
        progress=state.progress,
        message=state.message,
        report=state.report,
        error=error,
        created_at=state.created_at,
        deadline_at=state.deadline_at,
        updated_at=state.updated_at,
    )


async def stream_job_states(
    store: DurableJobStore,
    job_id: UUID,
    *,
    last_event_id: int = 0,
    heartbeat_interval: float = 20,
    timeout: float = 1260,
) -> AsyncGenerator[JobState | object, None]:
    """Subscribe before reading the snapshot, then recover solely by revision."""

    pubsub = store.redis.pubsub()
    started = time.monotonic()
    last_revision = max(last_event_id, 0)
    await pubsub.subscribe(store.keys.events(job_id))
    try:
        current = await store.require_state(job_id)
        if current.revision > last_revision:
            last_revision = current.revision
            yield current
        if current.terminal:
            return

        while time.monotonic() - started < timeout:
            message = await pubsub.get_message(
                ignore_subscribe_messages=True,
                timeout=heartbeat_interval,
            )
            if message is None:
                yield SSE_HEARTBEAT
                await asyncio.sleep(0)
                continue
            current = await store.require_state(job_id)
            if current.revision <= last_revision:
                continue
            last_revision = current.revision
            yield current
            if current.terminal:
                return
    finally:
        await pubsub.unsubscribe(store.keys.events(job_id))
        await pubsub.aclose()


def _job_error(exc: DurableJobError) -> HTTPException:
    if isinstance(exc, JobNotFound):
        status_code = status.HTTP_404_NOT_FOUND
    elif isinstance(exc, (IdempotencyConflict, JobIdentityConflict, JobNotRetryable, JobNewAttemptRequired)):
        status_code = status.HTTP_409_CONFLICT
    else:
        status_code = status.HTTP_400_BAD_REQUEST
    return HTTPException(
        status_code=status_code,
        detail={"code": exc.error_code, "message": exc.user_message},
    )


@router.head("/capability-analyze", status_code=204)
async def capability_readiness(
    _: Annotated[None, Depends(require_internal_auth)],
    __: Annotated[None, Depends(require_v22_capability_enabled)],
    runtime: Annotated[V22JobRuntime, Depends(get_v22_runtime)],
) -> Response:
    if runtime.capability_repository is None:
        raise HTTPException(status_code=503, detail={"code": "V22_CAPABILITY_NOT_READY", "message": "This analysis workflow is not available yet."})
    return Response(status_code=204)


@router.post("/capability-acquisition", response_model=TaskStatusResponse | CapabilityTaskStatusResponse, status_code=202)
async def submit_capability_acquisition(
    body: Annotated[AnalyzeRequest, Depends(parse_acquisition_request)],
    user_id: Annotated[UUID, Header(alias="X-SearchTrust-User-ID")],
    job_id: Annotated[UUID, Header(alias="X-SearchTrust-Job-ID")],
    workflow_id: Annotated[UUID, Header(alias="X-SearchTrust-Workflow-ID")],
    discovery_id: Annotated[UUID, Header(alias="X-SearchTrust-Discovery-ID")],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8,max_length=200,pattern=r"^[A-Za-z0-9._:-]+$")],
    _: Annotated[None, Depends(require_internal_auth)],
    __: Annotated[None, Depends(require_v22_acquisition_enabled)],
    runtime: Annotated[V22JobRuntime, Depends(get_v22_runtime)],
    previous_job_id: Annotated[UUID | None, Header(alias="X-SearchTrust-Previous-Job-ID")] = None,
    planning_revision: Annotated[int | None, Header(alias="X-SearchTrust-Planning-Revision", ge=0, le=2147483647)] = None,
):
    if runtime.capability_repository is None or runtime.acquisition_queue is None or runtime.discovery_verifier is None:
        raise HTTPException(status_code=503,detail={"code":"CAPABILITY_INTAKE_UNAVAILABLE","message":"This workflow is not ready."})
    try:
        return await submit_acquisition(repository=runtime.capability_repository,verifier=runtime.discovery_verifier,
            queue=runtime.acquisition_queue,user_id=user_id,job_id=job_id,
            workflow_id=workflow_id,discovery_id=discovery_id,idempotency_key=idempotency_key,
            previous_job_id=previous_job_id,request=body,now=datetime.now(timezone.utc),planning_revision=planning_revision)
    except DurableJobError as exc:
        raise HTTPException(status_code=503 if exc.retryable else 409,detail={"code":"CAPABILITY_INTAKE_UNAVAILABLE",
            "message":"This analysis could not be started or checked yet."}) from exc
    except (RedisError,OSError) as exc:
        raise HTTPException(status_code=503,detail={"code":"QUEUE_UNAVAILABLE","message":"The analysis queue is temporarily unavailable."}) from exc


@router.post("/capability-analyze", response_model=TaskStatusResponse | CapabilityTaskStatusResponse, status_code=202)
async def submit_capability_analysis(
    body: Annotated[CapabilityRequestEnvelope, Depends(parse_capability_request)],
    user_id: Annotated[UUID, Header(alias="X-SearchTrust-User-ID")],
    job_id: Annotated[UUID, Header(alias="X-SearchTrust-Job-ID")],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=200, pattern=r"^[A-Za-z0-9._:-]+$")],
    _: Annotated[None, Depends(require_internal_auth)],
    __: Annotated[None, Depends(require_v22_capability_enabled)],
    runtime: Annotated[V22JobRuntime, Depends(get_v22_runtime)],
):
    try:
        return await runtime.submit_capability(user_id=user_id, job_id=job_id,
                                              idempotency_key=idempotency_key, request=body)
    except DurableJobError as exc:
        raise HTTPException(status_code=503 if exc.retryable else 409, detail={
            "code": "CAPABILITY_DISPATCH_UNAVAILABLE", "message": "The prepared analysis could not be started or checked yet.",
        }) from exc
    except (RedisError, OSError) as exc:
        raise HTTPException(status_code=503, detail={
            "code": "QUEUE_UNAVAILABLE", "message": "The analysis queue is temporarily unavailable.",
        }) from exc


@router.head("/verified-analyze", status_code=status.HTTP_204_NO_CONTENT)
async def verified_analysis_readiness(
    _: Annotated[None, Depends(require_internal_auth)],
    __: Annotated[None, Depends(require_v22_verified_analysis_enabled)],
) -> Response:
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/analyze",
    response_model=TaskCreateResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def submit_analysis(
    body: Annotated[AnalyzeRequest, Depends(parse_analyze_request)],
    job_id: Annotated[UUID, Header(alias="X-SearchTrust-Job-ID")],
    idempotency_key: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=8, max_length=200, pattern=r"^[A-Za-z0-9._:-]+$"),
    ],
    _: Annotated[None, Depends(require_internal_auth)],
    __: Annotated[None, Depends(require_v22_analyze_enabled)],
    runtime: Annotated[V22JobRuntime, Depends(get_v22_runtime)],
    discovery_id: Annotated[UUID | None, Header(alias="X-SearchTrust-Discovery-ID")] = None,
) -> TaskCreateResponse:
    try:
        return await runtime.submit(
            job_id=job_id,
            idempotency_key=idempotency_key,
            request=body,
            discovery_id=discovery_id,
        )
    except DurableJobError as exc:
        raise _job_error(exc) from exc
    except (RedisError, OSError) as exc:
        logger.warning("v2.2 queue unavailable while submitting job_id=%s", job_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "QUEUE_UNAVAILABLE", "message": "The durable task queue is unavailable."},
        ) from exc


@router.post(
    "/verified-analyze",
    response_model=TaskCreateResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def submit_verified_analysis(
    body: Annotated[VerifiedTaskRequest, Depends(parse_verified_task_request)],
    job_id: Annotated[UUID, Header(alias="X-SearchTrust-Job-ID")],
    idempotency_key: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=8, max_length=200, pattern=r"^[A-Za-z0-9._:-]+$"),
    ],
    _: Annotated[None, Depends(require_internal_auth)],
    __: Annotated[None, Depends(require_v22_verified_analysis_enabled)],
    runtime: Annotated[V22JobRuntime, Depends(get_v22_runtime)],
) -> TaskCreateResponse:
    try:
        return await runtime.submit_verified(
            job_id=job_id,
            idempotency_key=idempotency_key,
            request=body,
        )
    except DurableJobError as exc:
        raise _job_error(exc) from exc
    except (RedisError, OSError) as exc:
        logger.warning("v2.2 queue unavailable while submitting verified job_id=%s", job_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "QUEUE_UNAVAILABLE", "message": "The durable task queue is unavailable."},
        ) from exc


@router.get("/tasks/{job_id}", response_model=TaskStatusResponse | CapabilityTaskStatusResponse)
async def get_task(
    job_id: UUID,
    _: Annotated[None, Depends(require_internal_auth)],
    runtime: Annotated[V22JobRuntime, Depends(get_v22_runtime)],
) -> TaskStatusResponse | CapabilityTaskStatusResponse:
    try:
        return state_to_public(await runtime.store.require_state(job_id))
    except DurableJobError as exc:
        raise _job_error(exc) from exc
    except (RedisError, OSError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "QUEUE_UNAVAILABLE", "message": "The durable task queue is unavailable."},
        ) from exc


@router.post(
    "/tasks/{job_id}/retry",
    response_model=RetryTaskResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def retry_task(
    job_id: UUID,
    _: Annotated[None, Depends(require_internal_auth)],
    runtime: Annotated[V22JobRuntime, Depends(get_v22_runtime)],
) -> RetryTaskResponse:
    try:
        return await runtime.retry(job_id)
    except DurableJobError as exc:
        raise _job_error(exc) from exc
    except (RedisError, OSError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "QUEUE_UNAVAILABLE", "message": "The durable task queue is unavailable."},
        ) from exc


@router.get("/tasks/{job_id}/stream")
async def stream_task(
    job_id: UUID,
    _: Annotated[None, Depends(require_internal_auth)],
    runtime: Annotated[V22JobRuntime, Depends(get_v22_runtime)],
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    try:
        parsed_event_id = max(int(last_event_id or "0"), 0)
    except ValueError:
        parsed_event_id = 0
    try:
        await runtime.store.require_state(job_id)
    except DurableJobError as exc:
        raise _job_error(exc) from exc

    async def events() -> AsyncGenerator[str, None]:
        async for item in stream_job_states(
            runtime.store,
            job_id,
            last_event_id=parsed_event_id,
            heartbeat_interval=settings.TASK_STREAM_HEARTBEAT_INTERVAL,
            timeout=settings.TASK_STREAM_TIMEOUT,
        ):
            if item is SSE_HEARTBEAT:
                yield ": heartbeat\n\n"
                continue
            state = item
            public = state_to_public(state)
            yield (
                f"id: {state.revision}\n"
                "event: state\n"
                f"data: {public.model_dump_json()}\n\n"
            )

    return StreamingResponse(events(), media_type="text/event-stream")


@router.get("/health/queue")
async def queue_health(runtime: Annotated[V22JobRuntime, Depends(get_v22_runtime)]) -> dict[str, Any]:
    try:
        redis_connected = bool(await runtime.redis.ping())
        worker_alive = bool(await runtime.redis.exists(runtime.store.keys.worker_health))
        pending_callbacks = await runtime.store.pending_callback_count()
    except (RedisError, OSError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "QUEUE_UNAVAILABLE", "message": "The durable task queue is unavailable."},
        ) from exc
    return {
        "status": "ok" if redis_connected and worker_alive else "degraded",
        "redis_connected": redis_connected,
        "worker_alive": worker_alive,
        "pending_callbacks": pending_callbacks,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


async def create_v22_runtime() -> V22JobRuntime | None:
    dsn = _secret_value(settings.V22_REDIS_URL)
    if not dsn:
        return None
    pool = await create_pool(
        RedisSettings.from_dsn(dsn),
        default_queue_name=settings.V22_QUEUE_NAME,
    )
    await pool.ping()
    store = DurableJobStore(
        pool,
        prefix=settings.V22_REDIS_PREFIX,
        state_ttl_seconds=settings.V22_JOB_STATE_TTL_SECONDS,
        job_timeout_seconds=settings.V22_JOB_TIMEOUT_SECONDS,
    )
    queue = ArqJobQueue(pool, queue_name=settings.V22_QUEUE_NAME)
    competitor_store = CompetitorDiscoveryStore(
        pool,
        prefix=settings.V22_REDIS_PREFIX,
        state_ttl_seconds=settings.V22_COMPETITOR_STATE_TTL_SECONDS,
    )
    verifier = RedisDiscoveryVerifier(
        store=competitor_store,
        market_store=SharedMarketSnapshotStore(
            pool,
            prefix=settings.V22_REDIS_PREFIX,
            ttl_seconds=settings.V22_COMPETITOR_MARKET_TTL_SECONDS,
        ),
    )
    capability_client = httpx.AsyncClient(timeout=60) if v22_capability_execution_ready(settings) else None
    try:
        capability_repository = SupabaseCapabilityInputRepository(
            url=settings.V22_SUPABASE_URL, service_role_key=_secret_value(settings.V22_SUPABASE_SERVICE_ROLE_KEY),
            http_client=capability_client,
        ) if capability_client else None
    except Exception:
        if capability_client is not None:
            await capability_client.aclose()
        await pool.aclose()
        raise
    return V22JobRuntime(
        store=store,
        queue=queue,
        redis=pool,
        discovery_verifier=verifier,
        capability_repository=capability_repository,
        capability_http_client=capability_client,
        acquisition_queue=ArqAcquisitionPreparationQueue(pool,queue_name=settings.V22_QUEUE_NAME) if capability_repository else None,
    )


async def close_v22_runtime(runtime: V22JobRuntime | None) -> None:
    if runtime is not None:
        try:
            if runtime.capability_http_client is not None:
                await runtime.capability_http_client.aclose()
        finally:
            await runtime.redis.aclose()
