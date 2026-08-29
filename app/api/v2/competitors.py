"""Independent durable API for v2.2 competitor discovery."""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.responses import StreamingResponse
from redis.exceptions import RedisError

from app.api.v2.competitor_models import (
    CompetitorDiscoveryRequest,
    CompetitorDiscoveryRetryResponse,
    CompetitorDiscoveryStatusResponse,
    CompetitorDiscoveryTaskCreateResponse,
)
from app.api.v2.dependencies import (
    get_v22_competitor_runtime,
    parse_competitor_discovery_request,
    require_internal_auth,
    require_v22_competitor_discovery_enabled,
)
from app.api.v2.runtime import SSE_HEARTBEAT, stream_job_states
from app.competitors_v22.models import CompetitorDiscoveryJobState
from app.competitors_v22.runtime import CompetitorDiscoveryRuntime
from app.core.config import settings
from app.jobs_v22.errors import (
    DurableJobError,
    IdempotencyConflict,
    JobIdentityConflict,
    JobNotFound,
    JobNotRetryable,
)


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v2/competitors", tags=["SearchTrust v2 competitor discovery"])


def discovery_state_to_public(
    state: CompetitorDiscoveryJobState,
) -> CompetitorDiscoveryStatusResponse:
    return CompetitorDiscoveryStatusResponse(
        discovery_job_id=state.discovery_job_id,
        status=state.status,
        stage=state.stage,
        progress=state.progress,
        message=state.message,
        result=state.result,
        error=state.error,
        created_at=state.created_at,
        updated_at=state.updated_at,
    )


def _job_error(exc: DurableJobError) -> HTTPException:
    if isinstance(exc, JobNotFound):
        status_code = status.HTTP_404_NOT_FOUND
    elif isinstance(exc, (IdempotencyConflict, JobIdentityConflict, JobNotRetryable)):
        status_code = status.HTTP_409_CONFLICT
    else:
        status_code = status.HTTP_400_BAD_REQUEST
    return HTTPException(
        status_code=status_code,
        detail={"code": exc.error_code, "message": exc.user_message},
    )


def _queue_unavailable() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={"code": "QUEUE_UNAVAILABLE", "message": "The durable task queue is unavailable."},
    )


@router.post(
    "/discover",
    response_model=CompetitorDiscoveryTaskCreateResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def submit_competitor_discovery(
    body: Annotated[CompetitorDiscoveryRequest, Depends(parse_competitor_discovery_request)],
    discovery_job_id: Annotated[UUID, Header(alias="X-SearchTrust-Discovery-Job-ID")],
    idempotency_key: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=8, max_length=200, pattern=r"^[A-Za-z0-9._:-]+$"),
    ],
    _: Annotated[None, Depends(require_internal_auth)],
    __: Annotated[None, Depends(require_v22_competitor_discovery_enabled)],
    runtime: Annotated[CompetitorDiscoveryRuntime, Depends(get_v22_competitor_runtime)],
) -> CompetitorDiscoveryTaskCreateResponse:
    try:
        return await runtime.submit(
            discovery_job_id=discovery_job_id,
            idempotency_key=idempotency_key,
            request=body,
        )
    except DurableJobError as exc:
        raise _job_error(exc) from exc
    except (RedisError, OSError) as exc:
        logger.warning("competitor discovery queue unavailable job_id=%s", discovery_job_id)
        raise _queue_unavailable() from exc


@router.get("/tasks/{discovery_job_id}", response_model=CompetitorDiscoveryStatusResponse)
async def get_competitor_discovery_task(
    discovery_job_id: UUID,
    _: Annotated[None, Depends(require_internal_auth)],
    runtime: Annotated[CompetitorDiscoveryRuntime, Depends(get_v22_competitor_runtime)],
) -> CompetitorDiscoveryStatusResponse:
    try:
        return discovery_state_to_public(await runtime.store.require_state(discovery_job_id))
    except DurableJobError as exc:
        raise _job_error(exc) from exc
    except (RedisError, OSError) as exc:
        raise _queue_unavailable() from exc


@router.post(
    "/tasks/{discovery_job_id}/retry",
    response_model=CompetitorDiscoveryRetryResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def retry_competitor_discovery_task(
    discovery_job_id: UUID,
    _: Annotated[None, Depends(require_internal_auth)],
    runtime: Annotated[CompetitorDiscoveryRuntime, Depends(get_v22_competitor_runtime)],
) -> CompetitorDiscoveryRetryResponse:
    try:
        return await runtime.retry(discovery_job_id)
    except DurableJobError as exc:
        raise _job_error(exc) from exc
    except (RedisError, OSError) as exc:
        raise _queue_unavailable() from exc


@router.get("/tasks/{discovery_job_id}/stream")
async def stream_competitor_discovery_task(
    discovery_job_id: UUID,
    _: Annotated[None, Depends(require_internal_auth)],
    runtime: Annotated[CompetitorDiscoveryRuntime, Depends(get_v22_competitor_runtime)],
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    try:
        parsed_event_id = max(int(last_event_id or "0"), 0)
    except ValueError:
        parsed_event_id = 0
    try:
        await runtime.store.require_state(discovery_job_id)
    except DurableJobError as exc:
        raise _job_error(exc) from exc

    async def events() -> AsyncGenerator[str, None]:
        async for item in stream_job_states(
            runtime.store,
            discovery_job_id,
            last_event_id=parsed_event_id,
            heartbeat_interval=settings.TASK_STREAM_HEARTBEAT_INTERVAL,
            timeout=settings.TASK_STREAM_TIMEOUT,
        ):
            if item is SSE_HEARTBEAT:
                yield ": heartbeat\n\n"
                continue
            state = item
            public = discovery_state_to_public(state)
            yield f"id: {state.revision}\nevent: state\ndata: {public.model_dump_json()}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")
