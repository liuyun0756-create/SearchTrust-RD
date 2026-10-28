"""SQL leased review worker. Redis is a wakeup, never an entitlement or result."""
import asyncio
import logging
from datetime import datetime, timezone
from typing import Literal
from uuid import UUID, uuid4

from pydantic import Field
from app.jobs_v22.errors import DeterministicJobError, classify_job_exception
from app.jobs_v22.review_input_repository import ReviewInputRepository
from app.jobs_v22.review_report_persistence import ReviewCompletionService
from app.report_v22.capability import Checksum, Timestamp, instant
from app.report_v22.comparison_base import StrictModel
from app.report_v22.review_generation_models import checked
from app.report_v22.review_freeze_storage import decode

logger = logging.getLogger(__name__)


class ReviewRun(StrictModel):
    job_id: UUID
    user_id: UUID
    case_id: UUID
    run_generation: int = Field(ge=2)
    input_checksum: Checksum
    deadline_at: Timestamp


class ReviewWorkRepository(ReviewInputRepository):
    RPC_NAMES = {"pending_v22_review_generations", "claim_v22_review_dispatch", "claim_v22_review_run",
        "heartbeat_v22_review_generation", "finalize_v22_review_generation"}
    response_limit = 16384

    async def call(self, name, args):
        return decode(await self._rpc(name, args))

    async def pending(self):
        raw = await self.call("pending_v22_review_generations", {})
        try:
            if not isinstance(raw, list) or len(raw) > 50 or any(not isinstance(x, str) for x in raw):
                raise ValueError()
            ids = [UUID(x) for x in raw]
            if len(set(ids)) != len(ids): raise ValueError()
            return ids
        except (ValueError, TypeError): raise self.storage_error() from None

    async def dispatch(self, job_id, token):
        r = await self.call("claim_v22_review_dispatch", dict(p_job_id=str(job_id), p_token=str(token)))
        if type(r) is not bool: raise self.storage_error()
        return r

    async def claim(self, job_id, dispatch_token, run_token):
        r = await self._rpc("claim_v22_review_run", dict(p_job_id=str(job_id), p_dispatch_token=str(dispatch_token), p_run_token=str(run_token)))
        if r.strip() == b"null": return None
        try:
            run = checked(ReviewRun, r, 4096)
            if run.job_id != job_id: raise ValueError()
            return run
        except (ValueError, DeterministicJobError): raise self.storage_error() from None

    async def heartbeat(self, run, token):
        r = await self.call("heartbeat_v22_review_generation", dict(p_job_id=str(run.job_id),p_run_generation=run.run_generation,p_run_token=str(token)))
        if type(r) is not bool: raise self.storage_error()
        return r

    async def finish(self, run, token, fail):
        r = await self.call("finalize_v22_review_generation", dict(p_job_id=str(run.job_id),p_run_generation=run.run_generation,p_run_token=str(token),p_fail=fail))
        if r not in ("succeeded", "failed", "queued", "inactive"): raise self.storage_error()
        return r


class ReviewCoordinator:
    def __init__(self, *, work, inputs, reports):
        self.work, self.inputs = work, inputs
        self.completion = ReviewCompletionService(reports)

    async def execute(self, job_id, dispatch_token):
        token = uuid4()
        run = await self.work.claim(job_id, dispatch_token, token)
        if run is None: return
        async def heartbeat():
            while True:
                await asyncio.sleep(20)
                if not await self.work.heartbeat(run, token):
                    raise RuntimeError("Review execution lease lost")
        async def generate():
            frozen = await self.inputs.resolve(user_id=run.user_id, case_id=run.case_id, job_id=run.job_id,
                run_generation=run.run_generation, expected_checksum=run.input_checksum)
            if not await self.work.heartbeat(run, token): return
            # Deterministic assembly has no provider I/O. Completion independently
            # rebuilds and validates the saved input under the current generation.
            await self.completion.complete(frozen=frozen, run_generation=run.run_generation)
        beat = asyncio.create_task(heartbeat())
        task = asyncio.create_task(generate())
        try:
            remaining = min(max((instant(run.deadline_at)-datetime.now(timezone.utc)).total_seconds(), .001), 240)
            done, _ = await asyncio.wait([beat, task], timeout=remaining, return_when=asyncio.FIRST_COMPLETED)
            if not done: raise TimeoutError()
            if beat in done: await beat
            await task
        except asyncio.CancelledError:
            # Process shutdown leaves the lease to expire; never refund locally.
            raise
        except Exception as exc:
            failure = classify_job_exception(exc)
            try:
                # SQL first returns an already saved success after any lost ack.
                await self.work.finish(run, token, not failure.retryable and not isinstance(exc, TimeoutError))
            except Exception:
                logger.warning("review terminal check deferred job_id_suffix=%s", str(job_id)[-8:])
        finally:
            for t in (beat, task): t.cancel()
            await asyncio.gather(beat, task, return_exceptions=True)


async def reconcile_v22_review_generations(ctx):
    coordinator = ctx.get("review_generation_coordinator")
    if coordinator is None: return
    try:
        for job in await coordinator.work.pending():
            token = uuid4()
            try:
                if await coordinator.work.dispatch(job, token):
                    await ctx["redis"].enqueue_job("execute_v22_review_generation", str(job), str(token),
                        _job_id=f"v22:review:{job}:{token}", _queue_name=ctx["review_generation_queue"], _expires=120)
            except Exception:
                # Unknown enqueue is retried after the bounded SQL dispatch lease.
                logger.warning("review dispatch deferred job_id_suffix=%s", str(job)[-8:])
    except Exception:
        logger.warning("review reconciliation deferred")


async def execute_v22_review_generation(ctx, job_value, dispatch_value):
    coordinator = ctx.get("review_generation_coordinator")
    if coordinator is not None:
        await coordinator.execute(UUID(job_value), UUID(dispatch_value))
