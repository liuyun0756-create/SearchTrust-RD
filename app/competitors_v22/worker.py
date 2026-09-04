"""Worker execution for durable competitor-discovery tasks."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from arq import Retry

from app.api.v2.competitor_models import CompetitorDiscoveryError, CompetitorDiscoveryRequest
from app.competitors_v22.store import CompetitorDiscoveryStore
from app.jobs_v22.checkpoints import JobCheckpoints
from app.jobs_v22.digest import canonical_json_bytes
from app.jobs_v22.errors import DeterministicJobError, classify_job_exception


logger = logging.getLogger(__name__)


def _now(ctx: dict[str, Any]) -> datetime:
    clock = ctx.get("clock")
    return clock() if clock is not None else datetime.now(timezone.utc)


def _retry_delay_seconds(job_id: UUID, attempt_count: int) -> int:
    base = min(5 * (2 ** max(attempt_count - 1, 0)), 60)
    jitter = hashlib.sha256(f"{job_id}:{attempt_count}".encode()).digest()[0] % 4
    return base + jitter


async def execute_v22_competitor_discovery(
    ctx: dict[str, Any], discovery_job_id_value: str, run_generation: int
) -> None:
    discovery_job_id = UUID(discovery_job_id_value)
    store: CompetitorDiscoveryStore = ctx["competitor_discovery_store"]
    state = await store.require_state(discovery_job_id)
    if state.terminal or state.run_generation != run_generation:
        return

    attempt_count = state.attempt_count + 1
    started = _now(ctx)
    running = await store.transition(
        discovery_job_id,
        status="running",
        stage="collecting_market",
        progress=max(state.progress, 1),
        message="Collecting market search results.",
        now=started,
        attempt_count=attempt_count,
        heartbeat_at=started,
    )
    if not running.applied:
        return

    checkpoints = JobCheckpoints(
        ctx["redis"],
        prefix=store.keys.prefix,
        ttl_seconds=int(
            ctx.get("competitor_state_ttl_seconds", store.state_ttl_seconds)
        ),
    )

    async def progress(stage: str, percent: int, message: str) -> None:
        await store.transition(
            discovery_job_id,
            status="running",
            stage=stage,
            progress=percent,
            message=message,
            now=_now(ctx),
            heartbeat_at=_now(ctx),
        )

    service = ctx["competitor_discovery_service"]
    try:
        raw_request = await store.get_request(discovery_job_id)
        if raw_request is None:
            raise DeterministicJobError(
                "DISCOVERY_REQUEST_MISSING",
                "The persisted competitor discovery request is unavailable.",
            )
        request = CompetitorDiscoveryRequest.model_validate_json(canonical_json_bytes(raw_request))
        result = await service.discover(
            discovery_job_id=discovery_job_id,
            request=request,
            checkpoints=checkpoints,
            progress=progress,
        )
    except asyncio.CancelledError:
        logger.info("competitor discovery cancelled job_id=%s", discovery_job_id)
        raise
    except Exception as exc:
        await _finish_failure(ctx, running.state, exc)
        return

    await store.transition(
        discovery_job_id,
        status="succeeded",
        stage="completed",
        progress=100,
        message="Competitor discovery complete.",
        now=_now(ctx),
        result=result,
    )


async def _finish_failure(ctx: dict[str, Any], state, exc: BaseException) -> None:
    store: CompetitorDiscoveryStore = ctx["competitor_discovery_store"]
    failure = classify_job_exception(exc)
    max_attempts = int(ctx.get("max_attempts", 3))
    attempt_count = max(state.attempt_count, 1)
    logger.warning(
        "competitor discovery attempt failed job_id=%s attempt=%d error_code=%s retryable=%s",
        state.discovery_job_id,
        attempt_count,
        failure.error_code,
        failure.retryable,
    )
    if failure.retryable and attempt_count < max_attempts:
        await store.transition(
            state.discovery_job_id,
            status="queued",
            stage="queued",
            progress=state.progress,
            message="A temporary issue occurred. Competitor discovery will retry automatically.",
            now=_now(ctx),
            attempt_count=attempt_count,
        )
        raise Retry(defer=_retry_delay_seconds(state.discovery_job_id, attempt_count))

    error_code = "DISCOVERY_RETRY_EXHAUSTED" if failure.retryable else failure.error_code
    user_message = (
        "Competitor discovery could not be completed after multiple attempts. Please retry later."
        if failure.retryable
        else failure.user_message
    )
    error = CompetitorDiscoveryError(
        error_code=error_code,
        user_message=user_message,
        retryable=failure.retryable,
        stage="failed",
        diagnostic_id=uuid4(),
    )
    await store.transition(
        state.discovery_job_id,
        status="failed",
        stage="failed",
        progress=state.progress,
        message=user_message,
        now=_now(ctx),
        attempt_count=attempt_count,
        error=error,
    )
