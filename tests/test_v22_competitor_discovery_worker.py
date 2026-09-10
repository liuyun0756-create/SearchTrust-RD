from __future__ import annotations

import asyncio
from datetime import timedelta
from uuid import UUID

from arq import Retry
import fakeredis.aioredis
import pytest

from app.competitors_v22.store import CompetitorDiscoveryStore
from app.competitors_v22.worker import execute_v22_competitor_discovery
from app.jobs_v22.errors import TransientJobError
from test_v22_competitor_models import CASE_ID, JOB_ID, NOW, discovery_request, discovery_result


class SuccessfulService:
    def __init__(self) -> None:
        self.calls = 0
        self.progress = []

    async def discover(
        self,
        *,
        discovery_job_id,
        request,
        checkpoints,
        progress=None,
        cost_ledger=None,
    ):
        self.calls += 1
        if progress is not None:
            await progress("ranking_candidates", 70, "Ranking competitor candidates.")
        return discovery_result().model_copy(update={"discovery_id": discovery_job_id})


class CancellingService:
    async def discover(self, **kwargs):
        raise asyncio.CancelledError()


class TransientService:
    async def discover(self, **kwargs):
        raise TransientJobError("SERP_PROVIDER_UNAVAILABLE", "Market search is temporarily unavailable.")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def context(service):
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    store = CompetitorDiscoveryStore(redis, prefix="test:v22", state_ttl_seconds=604_800)
    registered = await store.register_job(
        discovery_job_id=JOB_ID,
        case_id=CASE_ID,
        idempotency_key="competitor-discovery-1",
        request_payload=discovery_request().model_dump(mode="json"),
        now=NOW,
    )
    return {
        "redis": redis,
        "competitor_discovery_store": store,
        "competitor_discovery_service": service,
        "max_attempts": 3,
        "competitor_state_ttl_seconds": 604_800,
        "clock": lambda: NOW + timedelta(seconds=1),
    }, store, registered.state


@pytest.mark.anyio
async def test_discovery_worker_persists_success_and_progress() -> None:
    ctx, store, initial = await context(SuccessfulService())

    await execute_v22_competitor_discovery(ctx, str(JOB_ID), initial.run_generation)

    state = await store.require_state(JOB_ID)
    assert state.status == "succeeded"
    assert state.result is not None
    assert state.progress == 100
    assert state.revision >= 4


@pytest.mark.anyio
async def test_discovery_worker_preserves_cancelled_run() -> None:
    ctx, store, initial = await context(CancellingService())

    with pytest.raises(asyncio.CancelledError):
        await execute_v22_competitor_discovery(ctx, str(JOB_ID), initial.run_generation)

    state = await store.require_state(JOB_ID)
    assert state.status == "running"
    assert state.error is None


@pytest.mark.anyio
async def test_discovery_worker_requeues_transient_failure() -> None:
    ctx, store, initial = await context(TransientService())

    with pytest.raises(Retry):
        await execute_v22_competitor_discovery(ctx, str(JOB_ID), initial.run_generation)

    state = await store.require_state(JOB_ID)
    assert state.status == "queued"
    assert state.attempt_count == 1


@pytest.mark.anyio
async def test_discovery_worker_safely_fails_when_request_is_missing() -> None:
    ctx, store, initial = await context(SuccessfulService())
    await ctx["redis"].delete(store.keys.request(JOB_ID))

    await execute_v22_competitor_discovery(ctx, str(JOB_ID), initial.run_generation)

    state = await store.require_state(JOB_ID)
    assert state.status == "failed"
    assert state.error is not None
    assert state.error.error_code == "DISCOVERY_REQUEST_MISSING"
