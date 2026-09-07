"""Durable execution for explicit GA4 sync requests; no periodic initiation."""
import asyncio
import logging
from datetime import date
from uuid import UUID

from app.jobs_v22.digest import request_digest
from .gsc import SyncError
from .ga4 import evaluate_health

logger = logging.getLogger(__name__)


async def reconcile_v22_ga4_syncs(ctx):
    repository = ctx.get("ga4_sync_repository")
    if repository is None:
        return
    try:
        for job_id in await repository.pending():
            await ctx["redis"].enqueue_job("execute_v22_ga4_sync", job_id,
                _job_id=f"ga4-sync:{job_id}", _queue_name=ctx["ga4_queue_name"])
    except Exception:
        logger.warning("GA4 sync dispatch deferred code=SYNC_DISPATCH_UNAVAILABLE")


async def execute_v22_ga4_sync(ctx, job_id: str):
    repository = ctx.get("ga4_sync_repository")
    if repository is None:
        return
    job = None
    try:
        job = await repository.claim(str(UUID(job_id)))
        if job is None:
            return
        async with asyncio.timeout(240):
            token = await ctx["ga4_token_broker"].access_token(job["connection_id"])
            snapshot = await ctx["ga4_provider"].collect(job["resource_id"], job["filter_hosts"], token,
                date.fromisoformat(job["coverage_end"]))
            del token
            health, reasons = evaluate_health(snapshot)
            payload = snapshot.model_dump(mode="json")
            await repository.finish(job, payload, request_digest(payload), health, reasons)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        error = exc if isinstance(exc, SyncError) else SyncError(
            "SYNC_TIMEOUT" if isinstance(exc, TimeoutError) else "SYNC_FAILED", isinstance(exc, TimeoutError))
        if job is not None:
            try:
                await repository.fail(job, error)
            except Exception:
                logger.warning("GA4 sync result deferred code=SYNC_STORAGE_UNAVAILABLE")
        logger.warning("GA4 sync attempt ended code=%s", error.code)
