"""Idempotent recovery of stale runs and undelivered state callbacks."""

from __future__ import annotations

from datetime import datetime, timedelta
import logging
from typing import Any, Protocol
from uuid import uuid4

from app.core.config import settings
from app.jobs_v22.callbacks import CallbackSynchronizer
from app.jobs_v22.models import JobErrorState, utc_now
from app.jobs_v22.queue import ArqJobQueue, JobQueue
from app.jobs_v22.store import DurableJobStore


logger = logging.getLogger(__name__)


class StateSynchronizer(Protocol):
    async def sync(self, job_id) -> bool: ...


async def reconcile_once(
    *,
    store: DurableJobStore,
    queue: JobQueue,
    synchronizer: StateSynchronizer | None,
    now: datetime,
    stale_seconds: int,
    max_attempts: int,
) -> None:
    if synchronizer is not None:
        for job_id in await store.list_pending_callbacks(limit=100):
            try:
                await synchronizer.sync(job_id)
            except Exception as exc:
                logger.warning(
                    "v2.2 callback reconciliation deferred job_id_suffix=%s error=%s",
                    str(job_id)[-8:],
                    type(exc).__name__,
                )

    cutoff = now - timedelta(seconds=stale_seconds)
    for job_id in await store.list_stale_jobs(cutoff, limit=100):
        state = await store.require_state(job_id)
        if state.terminal:
            continue
        if state.status == "running" and state.attempt_count >= max_attempts:
            error = JobErrorState(
                error_code="JOB_RETRY_EXHAUSTED",
                user_message="The analysis could not be completed after multiple attempts. Please retry later.",
                retryable=True,
                stage="failed",
                diagnostic_id=uuid4(),
            )
            updated = await store.transition(
                job_id,
                status="failed",
                stage="failed",
                progress=state.progress,
                message=error.user_message,
                now=now,
                error=error,
            )
        else:
            updated = await store.transition(
                job_id,
                status="queued",
                stage="queued",
                progress=state.progress,
                message="Queued for recovery after a lost worker heartbeat.",
                now=now,
                attempt_count=state.attempt_count,
            )
            if updated.applied:
                await queue.enqueue(job_id, updated.state.run_generation)
        if synchronizer is not None and updated.applied:
            try:
                await synchronizer.sync(job_id)
            except Exception as exc:
                logger.warning(
                    "v2.2 recovered state callback deferred job_id_suffix=%s error=%s",
                    str(job_id)[-8:],
                    type(exc).__name__,
                )


async def reconcile_v22_jobs(ctx: dict[str, Any]) -> None:
    store: DurableJobStore = ctx["store"]
    queue = ArqJobQueue(ctx["redis"], queue_name=settings.V22_QUEUE_NAME)
    synchronizer: CallbackSynchronizer | None = ctx.get("callback_synchronizer")
    await reconcile_once(
        store=store,
        queue=queue,
        synchronizer=synchronizer,
        now=utc_now(),
        stale_seconds=settings.V22_JOB_STALE_SECONDS,
        max_attempts=settings.V22_JOB_MAX_ATTEMPTS,
    )
