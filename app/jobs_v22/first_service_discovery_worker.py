"""SQL-authorized initial discovery with bounded, non-billable recovery.

The dedicated Redis namespace must never be fed to the legacy reconciler.
Missing registered state is uncertainty, not permission to reset provider costs.
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
import hashlib
import logging
from uuid import UUID, uuid4

from arq import Retry

from app.core.config import settings
from app.competitors_v22.discovery_service import competitor_discovery_input_digest
from app.competitors_v22.worker import execute_v22_competitor_discovery
from app.jobs_v22.cost_ledger import JobCostLedger
from app.jobs_v22.cost_models import pricing_catalog_from_settings
from app.jobs_v22.digest import request_digest
from app.jobs_v22.models import utc_now

logger = logging.getLogger(__name__)


class FirstServiceDiscoveryCoordinator:
    def __init__(self, *, repository, store, market_store, redis, queue_name):
        self.repository, self.store, self.market_store = repository, store, market_store
        self.redis, self.queue_name = redis, queue_name

    @staticmethod
    def key(work):
        return f"first-service-discovery:{work.workflow_id}:{work.discovery_job_id}"

    async def checked_state(self, work):
        state = await self.store.get_state(work.discovery_job_id)
        if state is None:
            return None
        expected = request_digest(work.request.model_dump(mode="json"))
        request = await self.store.get_request(work.discovery_job_id)
        if (state.discovery_job_id != work.discovery_job_id or state.case_id != work.case_id
                or state.run_generation != 1 or state.request_digest != expected
                or request is None or request_digest(request) != expected
                or state.idempotency_key_digest != "sha256:" + hashlib.sha256(self.key(work).encode()).hexdigest()):
            raise ValueError("First-service discovery identity mismatch")
        return state

    async def finish_if_terminal(self, work, state, now):
        if state.status == "failed":
            await self.repository.finish(work)
            return True
        if state.status != "succeeded":
            return False
        result = state.result
        if (result is None or result.discovery_id != work.discovery_job_id or result.case_id != work.case_id
                or result.input_digest != competitor_discovery_input_digest(work.request)
                or result.created_at > now or result.expires_at <= now):
            raise ValueError("First-service discovery result mismatch")
        shared = await self.market_store.get(input_digest=result.input_digest, now=now)
        if (shared is None or shared.snapshot_id != result.market_snapshot_id
                or shared.snapshot_checksum != result.market_snapshot_checksum):
            raise ValueError("First-service discovery market unavailable")
        await self.repository.finish(work, result)
        return True

    async def reconcile_one(self, workflow_id, now):
        work = await self.repository.claim(workflow_id, uuid4())
        if work is None or work.deadline_at <= now:
            return
        state = await self.checked_state(work)
        if state is None:
            if work.outcome != "dispatch":
                return  # One-shot marker exists: never recreate lost state/budget.
            await self.store.register_job(discovery_job_id=work.discovery_job_id, case_id=work.case_id,
                idempotency_key=self.key(work), request_payload=work.request.model_dump(mode="json"), now=now)
            state = await self.checked_state(work)
        if state is None or await self.finish_if_terminal(work, state, now):
            return
        if work.attempts >= 3 or (work.lease_until is not None and work.lease_until > now):
            return
        await self.redis.enqueue_job("execute_v22_first_service_discovery", str(work.workflow_id),
            str(work.discovery_job_id), _queue_name=self.queue_name,
            _job_id=f"{self.key(work)}:attempt:{work.attempts + 1}")


async def reconcile_v22_first_service_discovery(ctx):
    coordinator = ctx.get("first_service_discovery_coordinator")
    if coordinator is None:
        return
    try:
        await coordinator.repository.expire()
        pending = await coordinator.repository.pending()
    except Exception as exc:
        logger.warning("first-service discovery polling deferred error=%s", type(exc).__name__)
        return
    for workflow_id in pending:
        try:
            await coordinator.reconcile_one(workflow_id, ctx.get("clock", utc_now)())
        except Exception as exc:
            logger.warning("first-service discovery handoff deferred error=%s", type(exc).__name__)


async def execute_v22_first_service_discovery(ctx, workflow_id_value, discovery_id_value):
    coordinator = ctx.get("first_service_discovery_coordinator")
    if coordinator is None:
        return
    workflow_id, discovery_id, token = UUID(workflow_id_value), UUID(discovery_id_value), uuid4()
    repository = coordinator.repository
    execution = heartbeat = None
    claimed = False
    try:
        work = await repository.claim(workflow_id, token, discovery_job_id=discovery_id)
        if work is None:
            return
        claimed = True
        clock = ctx.get("clock", utc_now)
        remaining = (work.deadline_at - clock()).total_seconds()
        if remaining <= 0:
            return
        state = await coordinator.checked_state(work)
        if state is None or state.terminal:
            return  # Reconciler persists terminal results independently.
        ledger = JobCostLedger(coordinator.redis, prefix=coordinator.store.keys.prefix,
            job_id=work.cost_ledger_job_id, ttl_seconds=coordinator.store.state_ttl_seconds,
            pricing=pricing_catalog_from_settings(settings), job_created_at=work.cost_ledger_created_at,
            require_existing=work.attempts > 1 or state.attempt_count > 0)
        if (work.attempts > 1 or state.attempt_count > 0) and not await coordinator.redis.exists(ledger.key):
            return  # Never replenish a lost provider budget on takeover.
        await ledger.ensure()
        if not await repository.lease(workflow_id, token):
            return
        child = {**ctx, "competitor_discovery_store": coordinator.store,
                 "max_attempts": state.attempt_count + 4 - work.attempts,
                 "discovery_require_existing_cost_ledger": True,
                 "discovery_cost_ledger_job_id": work.cost_ledger_job_id,
                 "discovery_cost_ledger_created_at": work.cost_ledger_created_at,
                 "discovery_total_attempts": work.attempts}

        async def maintain():
            while True:
                await asyncio.sleep(ctx.get("first_service_discovery_heartbeat_seconds", 15))
                if not await repository.lease(workflow_id, token):
                    return

        # Deadline includes provider work. A failed/unknown lease heartbeat also
        # stops the run, rather than treating an unverified lease as permission.
        remaining = (work.deadline_at - clock()).total_seconds()
        if remaining <= 0:
            return
        async with asyncio.timeout(remaining):
            execution = asyncio.create_task(execute_v22_competitor_discovery(child, str(discovery_id), 1))
            heartbeat = asyncio.create_task(maintain())
            done, _ = await asyncio.wait((execution, heartbeat), return_when=asyncio.FIRST_COMPLETED)
            if heartbeat in done:
                await heartbeat
                return
            try:
                await execution
            except Retry:
                return  # SQL attempt budget and cron own the retry, not ARQ.
            if await repository.lease(workflow_id, token):
                state = await coordinator.checked_state(work)
                if state is not None:
                    await coordinator.finish_if_terminal(work, state, clock())
    except Exception as exc:
        # Unknown database/transport outcomes never become failure refunds here.
        logger.warning("first-service discovery execution deferred error=%s", type(exc).__name__)
    finally:
        for task in (execution, heartbeat):
            if task is not None:
                task.cancel()
        for task in (execution, heartbeat):
            if task is not None:
                with suppress(asyncio.CancelledError, Exception):
                    await task
        if claimed:
            try:
                await repository.lease(workflow_id, token, release=True)
            except Exception as exc:
                logger.warning("first-service discovery lease release deferred error=%s", type(exc).__name__)
