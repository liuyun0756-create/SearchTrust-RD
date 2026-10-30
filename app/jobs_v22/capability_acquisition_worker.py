"""Isolated preparation queue; original SQL task, deadline and cost ledger.

No public intake is mounted here. SQL leases fence source freezing; a lost
acknowledgement is recovered by claim's ready outcome, never by recollection.
"""
import asyncio
import logging
from copy import copy
from time import monotonic
from uuid import UUID, uuid4

from arq import Retry
from pydantic import Field, model_validator

from app.jobs_v22.capability_acquisition_preparation import (
    AcquisitionPreparation, begin_acquisition_preparation, parse_acquisition_request,
)
from app.jobs_v22.checkpoints import JobCheckpoints
from app.jobs_v22.cost_ledger import JobCostLedger
from app.jobs_v22.digest import canonical_json_bytes, verified_request_digest
from app.jobs_v22.errors import JobLeaseLost
from app.jobs_v22.models import utc_now
from app.report_v22.models import StrictModel

logger = logging.getLogger(__name__)


class ClaimedPreparation(StrictModel):
    outcome: str
    attempt_count: int = Field(ge=0, le=10)
    request: dict
    preparation: AcquisitionPreparation

    @model_validator(mode="after")
    def bound(self):
        receipt = self.preparation
        envelope = parse_acquisition_request(self.request)
        req = envelope.analyze_request
        if (self.outcome not in {"claimed", "ready"}
                or (self.outcome == "ready") != (receipt.dispatch is not None)
                or (self.outcome == "claimed" and self.attempt_count < 1)
                or receipt.request_checksum != verified_request_digest(envelope.model_dump(mode="json"))
                or receipt.case_id != req.case_id
                or receipt.business.site_url != str(req.business_identity.site_url)
                or receipt.business.primary_service != req.primary_service
                or receipt.business.target_market != req.target_market.display_name
                or sorted(receipt.business.competitor_sites) != sorted(str(c.website_url) for c in req.competitors)):
            raise ValueError("Invalid preparation claim")
        return self


async def enqueue_acquisition_preparation(*, preparer, queue, submit_dispatch, **identity):
    """Internal intake seam: SQL registration first; replay repairs enqueue gaps."""
    receipt, _ = await begin_acquisition_preparation(preparer.repository, **identity)
    if receipt.dispatch is not None:
        return await submit_dispatch(user_id=receipt.user_id, job_id=receipt.job_id,
            idempotency_key=receipt.idempotency_key, request=receipt.dispatch.request)
    await queue.enqueue(receipt.job_id, receipt.run_generation)
    return receipt


class LeaseCheckpoints(JobCheckpoints):
    """Separate preparation namespace; SQL is checked before checkpoint I/O."""
    def __init__(self, *args, guard, generation, **kwargs):
        super().__init__(*args, **kwargs)
        self.guard = guard
        # SQL, not a formal Redis JobState, owns the preparation generation.
        self.run_generation = generation

    async def get(self, job_id, checkpoint_key):
        await self.guard()
        return await super().get(job_id, checkpoint_key)

    async def save(self, job_id, checkpoint_key, value):
        await self.guard()
        # Each generation has a distinct namespace. NX preserves the first
        # checkpoint; the final source/lease validation is still mandatory.
        result = await self.redis.set(self.keys.checkpoint(job_id, checkpoint_key),
            canonical_json_bytes(value), ex=self.ttl_seconds, nx=True)
        return bool(result)


class LeasedPreparationRpc:
    """Wrap only the freeze call; all validation remains in the real repository."""
    def __init__(self, rpc, *, token, before_freeze, kind="acquisition"):
        self.rpc = rpc
        self.token = token
        self.before_freeze = before_freeze
        self.kind = kind

    def invalid(self):
        return self.rpc.invalid()

    async def post(self, name, body):
        if name == "prepare_v22_capability_analysis_input":
            counters = await self.before_freeze()
            return await self.rpc.post(f"freeze_v22_{self.kind}_preparation", dict(
                p_lease_token=str(self.token), p_cost_counters=counters, p_input=body))
        return await self.rpc.post(name, body)


async def prepare_v22_acquisition(ctx, job_id_value: str, run_generation: int):
    try:
        return await _prepare(ctx, job_id_value, run_generation)
    except (Retry, asyncio.CancelledError):
        raise
    except Exception as exc:
        logger.warning("acquisition preparation deferred error=%s", type(exc).__name__)
        raise Retry(defer=30) from None


async def prepare_v22_first_service(ctx, job_id_value: str, run_generation: int):
    try:
        return await _prepare(ctx, job_id_value, run_generation, kind="first_service")
    except (Retry, asyncio.CancelledError):
        raise
    except Exception as exc:
        logger.warning("first-service preparation deferred error=%s", type(exc).__name__)
        raise Retry(defer=30) from None


async def _prepare(ctx, job_id_value: str, run_generation: int, *, kind="acquisition"):
    preparer = ctx.get(f"capability_{kind}_preparer")
    submit_dispatch = ctx.get("capability_submit_dispatch")
    if preparer is None or submit_dispatch is None:
        # Never fall through to legacy execution when readiness is disabled.
        raise Retry(defer=30)
    job_id = UUID(job_id_value)
    token = uuid4()
    rpc = preparer.repository.rpc
    raw = await rpc.post(f"claim_v22_{kind}_preparation", dict(p_job_id=str(job_id),
        p_run_generation=run_generation, p_lease_token=str(token), p_max_attempts=ctx["max_attempts"]))
    if raw == {"outcome": "inactive"}:
        return
    if raw == {"outcome": "busy"}:
        raise Retry(defer=30)
    try:
        if kind == "first_service":
            from app.jobs_v22.capability_first_service_preparation import ClaimedFirstServicePreparation
            claim = ClaimedFirstServicePreparation.model_validate_json(canonical_json_bytes(raw))
        else:
            claim = ClaimedPreparation.model_validate_json(canonical_json_bytes(raw))
        receipt = claim.preparation
        if receipt.job_id != job_id or receipt.run_generation != run_generation:
            raise ValueError("Invalid generation")
    except (ValueError, TypeError, AttributeError):
        raise rpc.invalid() from None
    if claim.outcome == "ready":
        return await _handoff(submit_dispatch, receipt)

    prefix = ctx["store"].keys.prefix
    ledger = JobCostLedger(ctx["redis"], prefix=prefix, job_id=job_id,
        ttl_seconds=ctx["state_ttl_seconds"], pricing=ctx["cost_pricing"], job_created_at=receipt.created_at,
        require_existing=claim.attempt_count > 1 or bool(receipt.cost_counters))
    # The SQL attempt is recorded BEFORE budget initialization/provider work.
    # A crash here may defer recovery, but cannot reset a spent budget.
    await ledger.ensure()
    ledger.require_existing = True
    stop = asyncio.Event()
    lost = asyncio.Event()
    started = monotonic()
    recorded = False
    pulse_lock = asyncio.Lock()

    async def pulse():
        async with pulse_lock:
            if lost.is_set():
                raise JobLeaseLost()
            ok = await rpc.post(f"heartbeat_v22_{kind}_preparation", dict(p_job_id=str(job_id),
                p_run_generation=run_generation, p_lease_token=str(token),
                p_cost_counters=(await ledger.snapshot()).root))
            if ok is not True:
                lost.set()
                raise JobLeaseLost()

    async def record_attempt():
        nonlocal recorded
        if not recorded:
            await ledger.record_job_attempt(active_elapsed_ms=max(int((monotonic()-started)*1000), 0), phase="preparation")
            recorded = True

    async def before_freeze():
        stop.set()
        await pulse()
        await record_attempt()
        return (await ledger.snapshot()).root

    async def heartbeat():
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=min(ctx.get("heartbeat_seconds", 20), 20))
            except TimeoutError:
                try:
                    await pulse()
                except Exception:
                    lost.set()
                    return

    if receipt.deadline_at <= utc_now():
        raise Retry(defer=30)
    # Persist the initialized budget before the first external operation.
    await pulse()
    repository = copy(preparer.repository)
    repository.rpc = LeasedPreparationRpc(rpc, token=token, before_freeze=before_freeze, kind=kind)
    owned = copy(preparer)
    owned.repository = repository
    async def report_stage(stage):
        await pulse()
        ok = await rpc.post("advance_v22_acquisition_stage", dict(p_job_id=str(job_id),
            p_run_generation=run_generation, p_lease_token=str(token), p_stage=stage))
        if ok is not True:
            lost.set()
            raise JobLeaseLost()

    checkpoints = LeaseCheckpoints(ctx["redis"], prefix=f"{prefix}:{kind}:{run_generation}",
        ttl_seconds=ctx["state_ttl_seconds"], generation=run_generation, guard=pulse,
        progress_reporter=report_stage if kind == "acquisition" else None)
    heartbeat_task = asyncio.create_task(heartbeat())
    lost_task = asyncio.create_task(lost.wait())
    operation = asyncio.create_task(owned.prepare(user_id=receipt.user_id, job_id=job_id,
        idempotency_key=receipt.idempotency_key, business_version_id=receipt.business.id,
        request=claim.request, checkpoints=checkpoints, cost_ledger=ledger))
    try:
        remaining = max((receipt.deadline_at-utc_now()).total_seconds(), 0)
        done, _ = await asyncio.wait({operation, lost_task}, timeout=remaining, return_when=asyncio.FIRST_COMPLETED)
        if operation not in done or lost.is_set():
            raise JobLeaseLost()
        dispatch = operation.result()
        return await _handoff(submit_dispatch, receipt.model_copy(update={"dispatch": dispatch}))
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # Unknown freeze/handoff outcome is resolved by the next SQL claim.
        # Original deadline reconciliation, not this exception, owns refunds.
        logger.warning("acquisition handoff deferred job_id_suffix=%s error=%s",
            str(job_id)[-8:], type(exc).__name__)
        raise Retry(defer=30) from None
    finally:
        stop.set()
        for task in (operation, heartbeat_task, lost_task):
            task.cancel()
        await asyncio.gather(operation, heartbeat_task, lost_task, return_exceptions=True)
        if not recorded:
            try:
                await record_attempt()
                await pulse()
            except Exception:
                pass  # Do not mask cancellation; SQL deadline remains authoritative.


async def _handoff(submit_dispatch, receipt):
    return await submit_dispatch(user_id=receipt.user_id, job_id=receipt.job_id,
        idempotency_key=receipt.idempotency_key, request=receipt.dispatch.request)
