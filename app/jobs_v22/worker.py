"""ARQ worker entrypoint for isolated SearchTrust v2.2 durable tasks."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

from arq import Retry
from arq.connections import RedisSettings
from arq.worker import func

from app.core.config import settings
from app.jobs_v22.checkpoints import JobCheckpoints
from app.jobs_v22.errors import DeterministicJobError, classify_job_exception
from app.jobs_v22.executor import UnavailableV22Executor
from app.jobs_v22.models import JobErrorState, utc_now
from app.jobs_v22.store import DurableJobStore


logger = logging.getLogger(__name__)


def retry_delay_seconds(job_id: UUID, attempt_count: int) -> int:
    """Bounded exponential delay with stable per-job jitter."""

    base = min(5 * (2 ** max(attempt_count - 1, 0)), 60)
    jitter = hashlib.sha256(f"{job_id}:{attempt_count}".encode()).digest()[0] % 4
    return base + jitter


async def execute_v22_job(ctx: dict[str, Any], job_id_value: str, run_generation: int) -> None:
    """Execute one physical run while Redis controls logical state and idempotency."""

    job_id = UUID(job_id_value)
    store: DurableJobStore = ctx["store"]
    state = await store.require_state(job_id)
    if state.terminal or state.run_generation != run_generation:
        return

    request = await store.get_request(job_id)
    if request is None:
        exc: BaseException = DeterministicJobError(
            "JOB_REQUEST_MISSING",
            "The persisted analysis request is unavailable.",
        )
        await _finish_failure(ctx, state, exc)
        return

    attempt_count = state.attempt_count + 1
    started = utc_now()
    running = await store.transition(
        job_id,
        status="running",
        stage="collecting_site",
        progress=max(state.progress, 1),
        message="Analysis is running.",
        now=started,
        attempt_count=attempt_count,
        heartbeat_at=started,
    )
    if not running.applied:
        return

    checkpoints = JobCheckpoints(
        ctx["redis"],
        prefix=store.keys.prefix,
        ttl_seconds=int(ctx.get("state_ttl_seconds", settings.V22_JOB_STATE_TTL_SECONDS)),
    )
    executor = ctx["executor"]
    try:
        report = await executor.execute(job_id=job_id, request=request, checkpoints=checkpoints)
    except asyncio.CancelledError:
        logger.info("v2.2 worker execution cancelled job_id=%s", job_id)
        raise
    except Exception as exc:  # ARQ must receive Retry for classified transient failures.
        await _finish_failure(ctx, running.state, exc)
        return


    await store.transition(
        job_id,
        status="succeeded",
        stage="completed",
        progress=100,
        message="Analysis complete.",
        now=utc_now(),
        report=report,
    )


async def _finish_failure(ctx: dict[str, Any], state, exc: BaseException) -> None:
    store: DurableJobStore = ctx["store"]
    failure = classify_job_exception(exc)
    max_attempts = int(ctx.get("max_attempts", settings.V22_JOB_MAX_ATTEMPTS))
    attempt_count = max(state.attempt_count, 1)

    if failure.retryable and attempt_count < max_attempts:
        await store.transition(
            state.job_id,
            status="queued",
            stage="queued",
            progress=state.progress,
            message="A temporary issue occurred. The task will retry automatically.",
            now=utc_now(),
            attempt_count=attempt_count,
        )
        raise Retry(defer=retry_delay_seconds(state.job_id, attempt_count))

    error_code = "JOB_RETRY_EXHAUSTED" if failure.retryable else failure.error_code
    user_message = (
        "The analysis could not be completed after multiple attempts. Please retry later."
        if failure.retryable
        else failure.user_message
    )
    error = JobErrorState(
        error_code=error_code,
        user_message=user_message,
        retryable=failure.retryable,
        stage="failed",
        diagnostic_id=uuid4(),
    )
    await store.transition(
        state.job_id,
        status="failed",
        stage="failed",
        progress=state.progress,
        message=user_message,
        now=utc_now(),
        attempt_count=attempt_count,
        error=error,
    )


async def on_startup(ctx: dict[str, Any]) -> None:
    pool = ctx["redis"]
    ctx["store"] = DurableJobStore(
        pool,
        prefix=settings.V22_REDIS_PREFIX,
        state_ttl_seconds=settings.V22_JOB_STATE_TTL_SECONDS,
    )
    ctx["executor"] = UnavailableV22Executor()
    ctx["max_attempts"] = settings.V22_JOB_MAX_ATTEMPTS
    ctx["state_ttl_seconds"] = settings.V22_JOB_STATE_TTL_SECONDS


def _redis_settings() -> RedisSettings:
    dsn = settings.V22_REDIS_URL.get_secret_value()
    return RedisSettings.from_dsn(dsn) if dsn else RedisSettings()


class WorkerSettings:
    functions = [
        func(
            execute_v22_job,
            name="execute_v22_job",
            max_tries=settings.V22_JOB_MAX_ATTEMPTS,
            timeout=settings.V22_JOB_TIMEOUT_SECONDS,
            keep_result=settings.V22_JOB_STATE_TTL_SECONDS,
        )
    ]
    on_startup = on_startup
    redis_settings = _redis_settings()
    queue_name = settings.V22_QUEUE_NAME
    max_jobs = settings.V22_WORKER_CONCURRENCY
    job_timeout = settings.V22_JOB_TIMEOUT_SECONDS
    max_tries = settings.V22_JOB_MAX_ATTEMPTS
    keep_result = settings.V22_JOB_STATE_TTL_SECONDS
    health_check_interval = settings.V22_JOB_HEARTBEAT_SECONDS
    health_check_key = f"{settings.V22_REDIS_PREFIX}:worker-health"
    retry_jobs = True
    job_completion_wait = 20
