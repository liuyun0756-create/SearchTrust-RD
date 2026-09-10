"""ARQ dispatch only for durable user requests; never initiates periodic syncs."""
import asyncio
import logging
from datetime import date
from uuid import UUID

from app.jobs_v22.digest import request_digest
from .gsc import SyncError, evaluate_health
from .sync_cost import attempt_timer, build_sync_cost_ledger, finish_attempt

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
    ledger = None
    attempt_started = None
    cost_counters = None
    try:
        job = await repository.claim(str(UUID(job_id)))
        if job is None:
            return
        ledger = build_sync_cost_ledger(ctx, job)
        if ledger is not None:
            await ledger.ensure()
        attempt_started = attempt_timer()
        async with asyncio.timeout(240):
            token = await ctx["gsc_token_broker"].access_token(job["connection_id"])
            snapshot = await ctx["gsc_provider"].collect(
                job["resource_id"],
                token,
                date.fromisoformat(job["coverage_end"]),
                cost_ledger=ledger,
            )
            del token
            health, reasons = evaluate_health(snapshot)
            payload = snapshot.model_dump(mode="json")
            cost_counters = await finish_attempt(ledger, attempt_started)
            attempt_started = None
            await repository.finish(
                job,
                payload,
                request_digest(payload),
                health,
                reasons,
                cost_counters=cost_counters,
            )
    except asyncio.CancelledError:
        if attempt_started is not None:
            await finish_attempt(ledger, attempt_started)
        # Lease expiry recovers interrupted execution; do not mark a cancelled run successful.
        raise
    except Exception as exc:
        if attempt_started is not None:
            cost_counters = await finish_attempt(ledger, attempt_started)
            attempt_started = None
        error = exc if isinstance(exc, SyncError) else SyncError("SYNC_TIMEOUT" if isinstance(exc, TimeoutError) else "SYNC_FAILED", isinstance(exc, TimeoutError))
        if job is not None:
            try:
                await repository.fail(job, error, cost_counters=cost_counters)
            except Exception:
                logger.warning("GSC sync result deferred code=SYNC_STORAGE_UNAVAILABLE")
        logger.warning("GSC sync attempt ended code=%s", error.code)
