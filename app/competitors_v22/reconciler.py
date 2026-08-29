"""Recovery of competitor-discovery tasks with lost queue or worker activity."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from app.api.v2.competitor_models import CompetitorDiscoveryError
from app.competitors_v22.queue import ArqCompetitorDiscoveryQueue, CompetitorDiscoveryQueue
from app.competitors_v22.store import CompetitorDiscoveryStore
from app.core.config import settings
from app.jobs_v22.models import utc_now


async def reconcile_competitor_discoveries_once(
    *,
    store: CompetitorDiscoveryStore,
    queue: CompetitorDiscoveryQueue,
    now: datetime,
    stale_seconds: int,
    max_attempts: int,
) -> None:
    cutoff = now - timedelta(seconds=stale_seconds)
    for discovery_job_id in await store.list_stale_jobs(cutoff, limit=100):
        state = await store.require_state(discovery_job_id)
        if state.terminal:
            continue
        if state.status == "running" and state.attempt_count >= max_attempts:
            error = CompetitorDiscoveryError(
                error_code="DISCOVERY_RETRY_EXHAUSTED",
                user_message=(
                    "Competitor discovery could not be completed after multiple attempts. "
                    "Please retry later."
                ),
                retryable=True,
                stage="failed",
                diagnostic_id=uuid4(),
            )
            await store.transition(
                discovery_job_id,
                status="failed",
                stage="failed",
                progress=state.progress,
                message=error.user_message,
                now=now,
                error=error,
            )
            continue
        updated = await store.transition(
            discovery_job_id,
            status="queued",
            stage="queued",
            progress=state.progress,
            message="Queued for recovery after lost worker activity.",
            now=now,
            attempt_count=state.attempt_count,
        )
        if updated.applied:
            await queue.enqueue(discovery_job_id, updated.state.run_generation)


async def reconcile_v22_competitor_discoveries(ctx: dict[str, Any]) -> None:
    store: CompetitorDiscoveryStore = ctx["competitor_discovery_store"]
    queue = ArqCompetitorDiscoveryQueue(ctx["redis"], queue_name=settings.V22_QUEUE_NAME)
    await reconcile_competitor_discoveries_once(
        store=store,
        queue=queue,
        now=utc_now(),
        stale_seconds=settings.V22_JOB_STALE_SECONDS,
        max_attempts=settings.V22_JOB_MAX_ATTEMPTS,
    )
