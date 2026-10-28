from datetime import timedelta
import json
from uuid import uuid4

import fakeredis.aioredis
import httpx
import pytest
from arq import Retry

from app.jobs_v22.capability_input_repository import SupabaseCapabilityInputRepository
from app.jobs_v22.capability_runtime_models import CapabilityRequestEnvelope
from app.jobs_v22.capability_terminal import CapabilityTerminalCoordinator
from app.jobs_v22.models import utc_now
from app.jobs_v22.reconciler import reconcile_once
from app.jobs_v22.store import DurableJobStore
from app.jobs_v22.worker import execute_v22_job
from test_v22_capability_input_repository import graph
from test_api_v2_jobs import RecordingQueue


@pytest.fixture
def anyio_backend():return "asyncio"


@pytest.mark.anyio
@pytest.mark.parametrize("driver",["worker","reconciler"])
@pytest.mark.parametrize("sql",["succeeded","failed","unavailable"])
async def test_deadline_never_invents_a_refund_or_redis_failure_when_sql_outcome_is_unknown(driver,sql):
    m,_,_,task,raw=graph(completed=True)
    redis=fakeredis.aioredis.FakeRedis(decode_responses=False)
    store=DurableJobStore(redis,prefix="capability-terminal",state_ttl_seconds=3600)
    now=utc_now()
    envelope=CapabilityRequestEnvelope(schema_version="v22_capability_request_envelope_v1",capability_request=task)
    await store.register_job(job_id=m.job_id,case_id=m.case_id,idempotency_key="one-workflow",request_payload=envelope.model_dump(mode="json"),now=now-timedelta(hours=1))
    async def respond(sent):
        assert sent.url.path.endswith("/finalize_v22_capability_failure")
        assert json.loads(sent.content)["p_fail"] is True
        if sql=="unavailable":return httpx.Response(503,text="private diagnostics")
        return httpx.Response(200,json=dict(status=sql,completed_report=raw["completed_report"] if sql=="succeeded" else None))
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        coordinator=CapabilityTerminalCoordinator(SupabaseCapabilityInputRepository(url="https://storage.example",service_role_key="test-key",http_client=client))
        if driver=="worker":
            ctx=dict(redis=redis,store=store,capability_terminal_coordinator=coordinator,max_attempts=3,state_ttl_seconds=3600)
            if sql=="unavailable":
                with pytest.raises(Retry):await execute_v22_job(ctx,str(m.job_id),1)
            else:await execute_v22_job(ctx,str(m.job_id),1)
        else:
            await reconcile_once(store=store,queue=RecordingQueue(),synchronizer=None,now=now,stale_seconds=30,max_attempts=3,capability_terminal=coordinator)
        state=await store.require_state(m.job_id)
        assert state.status== ("queued" if sql=="unavailable" else sql)
        if sql=="succeeded":assert state.report is not None and state.error is None
        if sql=="failed":assert state.report is None and state.error.error_code=="JOB_DEADLINE_EXCEEDED"
    await redis.aclose()


@pytest.mark.anyio
async def test_stale_reconciler_recovers_sql_success_before_increasing_a_generation():
    m,_,_,task,raw=graph(completed=True)
    redis=fakeredis.aioredis.FakeRedis(decode_responses=False)
    store=DurableJobStore(redis,prefix="capability-stale",state_ttl_seconds=3600)
    now=utc_now()
    envelope=CapabilityRequestEnvelope(schema_version="v22_capability_request_envelope_v1",capability_request=task)
    await store.register_job(job_id=m.job_id,case_id=m.case_id,idempotency_key="one-workflow",request_payload=envelope.model_dump(mode="json"),now=now-timedelta(minutes=5))
    await store.transition(m.job_id,status="running",stage="collecting_site",progress=20,message="Running",now=now-timedelta(minutes=4),attempt_count=1)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda sent:httpx.Response(200,json=dict(status="succeeded",completed_report=raw["completed_report"])))) as client:
        coordinator=CapabilityTerminalCoordinator(SupabaseCapabilityInputRepository(url="https://storage.example",service_role_key="test-key",http_client=client))
        queue=RecordingQueue()
        await reconcile_once(store=store,queue=queue,synchronizer=None,now=now,stale_seconds=30,max_attempts=3,capability_terminal=coordinator)
        result=await store.require_state(m.job_id)
        assert result.status=="succeeded" and result.run_generation==1 and queue.calls==[]
    await redis.aclose()
