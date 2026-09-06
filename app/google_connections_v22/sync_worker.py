"""ARQ dispatch only for durable user requests; never initiates periodic syncs."""
import asyncio
import logging
from datetime import date
from uuid import UUID

from app.jobs_v22.digest import request_digest
from .gsc import SyncError, evaluate_health

logger = logging.getLogger(__name__)


async def reconcile_v22_gsc_syncs(ctx):
    repository = ctx.get("gsc_sync_repository")
    if repository is None:
        return
    try:
        for job_id in await repository.pending():
            await ctx["redis"].enqueue_job("execute_v22_gsc_sync", job_id,
                _job_id=f"gsc-sync:{job_id}", _queue_name=ctx["gsc_queue_name"])
    except Exception:
        # No exception interpolation: HTTP request/response objects may carry secrets.
        logger.warning("GSC sync dispatch deferred code=SYNC_DISPATCH_UNAVAILABLE")


async def execute_v22_gsc_sync(ctx, job_id: str):
    repository = ctx.get("gsc_sync_repository")
    if repository is None:
        return
    job = None
    try:
        job = await repository.claim(str(UUID(job_id)))
        if job is None:
            return
        async with asyncio.timeout(240):
            token = await ctx["gsc_token_broker"].access_token(job["connection_id"])
            snapshot = await ctx["gsc_provider"].collect(job["resource_id"], token, date.fromisoformat(job["coverage_end"]))
            del token
            health, reasons = evaluate_health(snapshot)
            payload = snapshot.model_dump(mode="json")
            await repository.finish(job, payload, request_digest(payload), health, reasons)
    except asyncio.CancelledError:
        # Lease expiry recovers interrupted execution; do not mark a cancelled run successful.
        raise
    except Exception as exc:
        error = exc if isinstance(exc, SyncError) else SyncError("SYNC_TIMEOUT" if isinstance(exc, TimeoutError) else "SYNC_FAILED", isinstance(exc, TimeoutError))
        if job is not None:
            try:
                await repository.fail(job, error)
            except Exception:
                logger.warning("GSC sync result deferred code=SYNC_STORAGE_UNAVAILABLE")
        logger.warning("GSC sync attempt ended code=%s", error.code)
