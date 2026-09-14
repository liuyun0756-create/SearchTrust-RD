"""Idempotent recovery of stale runs and undelivered state callbacks."""

from __future__ import annotations

from datetime import datetime, timedelta
import logging
from typing import Any, Protocol
from uuid import UUID, uuid4

from app.core.config import settings
from app.jobs_v22.callbacks import CallbackSynchronizer
from app.jobs_v22.errors import JobNotFound
from app.jobs_v22.models import JobErrorState, utc_now
from app.jobs_v22.queue import ArqJobQueue, JobQueue
from app.jobs_v22.store import DurableJobStore


logger = logging.getLogger(__name__)


class StateSynchronizer(Protocol):
    async def sync(self, job_id) -> bool: ...


async def _finish_pending_recovery(
    *,
    store: DurableJobStore,
    queue: JobQueue,
    synchronizer: StateSynchronizer | None,
    job_id: UUID,
    generation: int,
    now: datetime,
) -> None:
    """Synchronize one takeover generation before exposing it to workers."""

    try:
        state = await store.require_state(job_id)
    except JobNotFound:
        if await store.discard_missing_job_recovery(job_id, generation=generation):
            logger.warning(
                "v2.2 orphan recovery marker removed job_id_suffix=%s error=JOB_NOT_FOUND",
                str(job_id)[-8:],
            )
        return
    if state.run_generation != generation:
        await store.discard_pending_recovery(job_id, generation=generation)
        return
    if state.terminal:
        await store.discard_pending_recovery(job_id, generation=generation)
        return
    if now >= state.deadline_at:
        error = JobErrorState(
            error_code="JOB_DEADLINE_EXCEEDED",
            user_message="The analysis exceeded its processing deadline.",
            retryable=False,
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
            expected_generation=generation,
        )
        if synchronizer is not None and updated.applied:
            try:
                await synchronizer.sync(job_id)
            except Exception as exc:
                logger.warning(
                    "v2.2 recovered state callback deferred job_id_suffix=%s error=%s",
                    str(job_id)[-8:],
                    type(exc).__name__,
                )
        return
    if synchronizer is None:
        return
    if state.callback_synced_revision < state.revision:
        try:
            if not await synchronizer.sync(job_id):
                return
        except Exception as exc:
            logger.warning(
                "v2.2 recovery callback deferred job_id_suffix=%s error=%s",
                str(job_id)[-8:],
                type(exc).__name__,
            )
            return
    try:
        # Queue implementations must deduplicate the physical job ID formed
        # from job_id + generation. False therefore means it already exists.
        await queue.enqueue(job_id, generation)
    except Exception as exc:
        logger.warning(
            "v2.2 recovery enqueue deferred job_id_suffix=%s error=%s",
            str(job_id)[-8:],
            type(exc).__name__,
        )
        return
    await store.mark_recovery_enqueued(job_id, generation=generation, now=now)


async def reconcile_once(
    *,
    store: DurableJobStore,
    queue: JobQueue,
    synchronizer: StateSynchronizer | None,
    now: datetime,
    stale_seconds: int,
    max_attempts: int,
) -> None:
    # A prior reconciler may have exited after Redis fenced the stale owner,
    # after the database callback, or after the idempotent physical enqueue.
    # Resume that exact generation before considering any new takeover.
    pending_recoveries = await store.list_pending_recoveries(limit=100)
    recovery_job_ids = {job_id for job_id, _ in pending_recoveries}
    for job_id, generation in pending_recoveries:
        await _finish_pending_recovery(
            store=store,
            queue=queue,
            synchronizer=synchronizer,
            job_id=job_id,
            generation=generation,
            now=now,
        )

    if synchronizer is not None:
        for job_id in await store.list_pending_callbacks(limit=100):
            if job_id in recovery_job_ids:
                # The recovery barrier made this cycle's one callback attempt.
                continue
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
        try:
            state = await store.require_state(job_id)
        except JobNotFound:
            # Missing recovery states are normally purged above. An orphan
            # active index without a marker must not abort the cron either.
            continue
        if state.terminal:
            continue
        pending_generation = await store.pending_recovery_generation(job_id)
        deadline_exceeded = now >= state.deadline_at
        if (
            pending_generation is not None
            and pending_generation != state.run_generation
        ):
            await store.discard_pending_recovery(
                job_id, generation=pending_generation
            )
            pending_generation = None
        if pending_generation is not None and not deadline_exceeded:
            # Never increment a generation that has not yet crossed the
            # database-sync-before-enqueue barrier.
            continue
        if deadline_exceeded or (state.status == "running" and state.attempt_count >= max_attempts):
            error = JobErrorState(
                error_code=("JOB_DEADLINE_EXCEEDED" if deadline_exceeded else "JOB_RETRY_EXHAUSTED"),
                user_message=(
                    "The analysis exceeded its processing deadline."
                    if deadline_exceeded
                    else "The analysis could not be completed after multiple attempts. Please retry later."
                ),
                retryable=not deadline_exceeded,
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
                expected_generation=state.run_generation,
            )
        else:
            updated = await store.take_over_stale(
                job_id,
                expected_generation=state.run_generation,
                now=now,
            )
            recovery_generation = await store.pending_recovery_generation(job_id)
            if recovery_generation is not None:
                await _finish_pending_recovery(
                    store=store,
                    queue=queue,
                    synchronizer=synchronizer,
                    job_id=job_id,
                    generation=recovery_generation,
                    now=now,
                )
        if synchronizer is not None and updated.applied and updated.state.terminal:
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
