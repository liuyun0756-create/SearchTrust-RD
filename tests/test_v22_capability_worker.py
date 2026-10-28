from copy import deepcopy
from datetime import timedelta
import json
from unittest.mock import AsyncMock

import fakeredis.aioredis
import httpx
import pytest
from arq import Retry
from pydantic import SecretStr

from app.api.v2.runtime import state_to_public
from app.core.config import Settings,settings,v22_capability_execution_ready
from app.jobs_v22.capability_executor import CapabilityV22Executor
from app.jobs_v22.capability_input_repository import SupabaseCapabilityInputRepository
from app.jobs_v22.capability_result_persistence import SupabaseCapabilityResultPersister
from app.jobs_v22.capability_terminal import CapabilityTerminalCoordinator
from app.jobs_v22.capability_runtime_models import CapabilityRequestEnvelope,CapabilityTaskStatusResponse
from app.jobs_v22.digest import verified_request_digest
from app.jobs_v22.executor import UnavailableV22Executor
from app.jobs_v22.store import DurableJobStore
from app.jobs_v22.verified_executor import V22ExecutorRouter
from app.jobs_v22.worker import execute_v22_job,on_startup,on_shutdown
from test_v22_capability_input_repository import graph
from test_v22_capability_public import inputs as public_inputs


@pytest.fixture
def anyio_backend(): return "asyncio"


@pytest.mark.anyio
@pytest.mark.parametrize("mode",["acquisition","first_service","after_acquisition"])
@pytest.mark.parametrize("lost_ack",[False,True,"last"])
async def test_actual_worker_generates_commits_and_recovers_sql_success_without_another_completion(mode,lost_ack,monkeypatch):
    m,request,parent,task,raw=graph(mode)
    import app.jobs_v22.worker as module
    monkeypatch.setattr(module,"utc_now",lambda: __import__("datetime").datetime.fromisoformat(m.evaluated_at.replace("Z","+00:00")))
    now=module.utc_now()
    committed=None; calls=[]
    def respond(sent):
        nonlocal committed
        calls.append(sent.url.path.rsplit("/",1)[-1])
        if calls[-1]=="resolve_v22_capability_analysis_input":
            body=deepcopy(raw)
            if committed is not None:body.update(request=None,previous_report=None,completed_report=committed)
            return httpx.Response(200,json=body)
        if calls[-1]=="finalize_v22_capability_failure":
            assert committed is not None
            return httpx.Response(200,json=dict(status="succeeded",completed_report=committed))
        assert calls[-1]=="complete_v22_capability_analysis"
        committed=json.loads(sent.content)["p_payload"]
        if lost_ack:raise httpx.ReadTimeout("response lost after SQL commit")
        return httpx.Response(200,json=[dict(report_id=str(m.job_id),payload_checksum=verified_request_digest(committed),idempotent=False)])
    redis=fakeredis.aioredis.FakeRedis(decode_responses=False)
    store=DurableJobStore(redis,prefix="capability-worker",state_ttl_seconds=3600)
    envelope=CapabilityRequestEnvelope(schema_version="v22_capability_request_envelope_v1",capability_request=task)
    await store.register_job(job_id=m.job_id,case_id=m.case_id,idempotency_key="one-reserved-analysis",request_payload=envelope.model_dump(mode="json"),now=now)
    if lost_ack=="last":
        from app.jobs_v22.cost_ledger import JobCostLedger
        from app.jobs_v22.cost_models import pricing_catalog_from_settings
        prior_ledger=JobCostLedger(redis,prefix=store.keys.prefix,job_id=m.job_id,ttl_seconds=3600,
            pricing=pricing_catalog_from_settings(settings),job_created_at=now)
        await prior_ledger.record_job_attempt(active_elapsed_ms=1)
        await prior_ledger.record_job_attempt(active_elapsed_ms=1)
        await store.transition(m.job_id,status="running",stage="collecting_site",progress=1,message="Running",now=now,attempt_count=2)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        executor=CapabilityV22Executor(resolver=SupabaseCapabilityInputRepository(url="https://storage.example",service_role_key="test-key",http_client=client),
            persister=SupabaseCapabilityResultPersister(url="https://storage.example",service_role_key="test-key",http_client=client))
        ctx=dict(redis=redis,store=store,executor=V22ExecutorRouter(prospect_executor=UnavailableV22Executor(),verified_executor=UnavailableV22Executor(),capability_executor=executor),
            capability_terminal_coordinator=CapabilityTerminalCoordinator(executor.resolver),max_attempts=3,state_ttl_seconds=3600)
        if lost_ack is True:
            with pytest.raises(Retry):await execute_v22_job(ctx,str(m.job_id),1)
            assert (await store.require_state(m.job_id)).status=="queued"
        await execute_v22_job(ctx,str(m.job_id),1)
        state=await store.require_state(m.job_id)
        assert state.status=="succeeded" and state.report.model_dump(mode="json")==committed
        assert state.cost_counters["provider_attempts_total"]==0
        await execute_v22_job(ctx,str(m.job_id),1)
        assert calls.count("complete_v22_capability_analysis")==1
        assert calls.count("resolve_v22_capability_analysis_input")== (2 if lost_ack is True else 1)
        assert calls.count("finalize_v22_capability_failure")== (1 if lost_ack=="last" else 0)
        public=state_to_public(state)
        assert isinstance(public,CapabilityTaskStatusResponse)
        payload=public.model_dump(mode="json")
        assert payload["report_reference"]["payload_checksum"]==verified_request_digest(committed)
        assert "report" not in payload and "input_manifest" not in json.dumps(payload) and "cost_counters" not in payload
    await redis.aclose()


@pytest.mark.anyio
async def test_new_envelope_is_not_silently_routed_to_a_legacy_executor_or_enabled_by_request():
    m,_,_,task,_=graph()
    old=AsyncMock()
    router=V22ExecutorRouter(prospect_executor=old,verified_executor=old)
    envelope=CapabilityRequestEnvelope(schema_version="v22_capability_request_envelope_v1",capability_request=task)
    from app.jobs_v22.errors import DeterministicJobError
    with pytest.raises(DeterministicJobError) as error:
        await router.execute(job_id=m.job_id,request=envelope,submitted_at=None,checkpoints=None)
    assert error.value.error_code=="V22_CAPABILITY_EXECUTION_DISABLED"
    old.execute.assert_not_awaited()


@pytest.mark.anyio
@pytest.mark.parametrize("enabled",[False,True])
@pytest.mark.parametrize("collection_enabled",[False,True])
@pytest.mark.parametrize("discovery_enabled",[False,True])
async def test_startup_injects_only_explicitly_enabled_capability_branch_and_closes_its_client(enabled,collection_enabled,discovery_enabled,monkeypatch):
    redis=fakeredis.aioredis.FakeRedis(decode_responses=False)
    for name in ("V22_ANALYZE_ENABLED","V22_VERIFIED_ANALYSIS_ENABLED","V22_GSC_SYNC_ENABLED","V22_GA4_SYNC_ENABLED","V22_GBP_SYNC_ENABLED"):
        monkeypatch.setattr(settings,name,False)
    monkeypatch.setattr(settings,"V22_ANALYZE_ENABLED",collection_enabled)
    monkeypatch.setattr(settings,"V22_CAPABILITY_EXECUTION_ENABLED",enabled)
    monkeypatch.setattr(settings,"V22_FIRST_SERVICE_DISCOVERY_ENABLED",discovery_enabled)
    monkeypatch.setattr(settings,"V22_SUPABASE_URL","https://storage.example")
    monkeypatch.setattr(settings,"V22_SUPABASE_SERVICE_ROLE_KEY",SecretStr("test-key"))
    monkeypatch.setattr(settings,"V22_CALLBACK_URL","https://app.example/callback")
    monkeypatch.setattr(settings,"V22_CALLBACK_SECRET",SecretStr("test-callback"))
    ctx=dict(redis=redis)
    await on_startup(ctx)
    try:
        executor=ctx["executor"].capability_executor
        assert (executor is not None)==enabled
        assert ("capability_acquisition_preparer" in ctx)==(enabled and collection_enabled)
        assert ("capability_submit_dispatch" in ctx)==(enabled and collection_enabled)
        assert ("first_service_discovery_coordinator" in ctx)==(enabled and collection_enabled and discovery_enabled)
        if enabled and collection_enabled and discovery_enabled:
            coordinator=ctx["first_service_discovery_coordinator"]
            assert coordinator.store.keys.prefix != ctx["competitor_discovery_store"].keys.prefix
            assert ctx["capability_first_service_preparer"].discovery_verifier.store is coordinator.store
            assert ctx["capability_acquisition_preparer"].discovery_verifier.store is ctx["competitor_discovery_store"]
        if enabled:
            assert isinstance(executor,CapabilityV22Executor)
            assert executor.resolver.rpc.client is ctx["capability_http_client"]
            assert executor.persister.rpc.client is ctx["capability_http_client"]
    finally:await on_shutdown(ctx)
    if enabled:assert ctx["capability_http_client"].is_closed


def test_capability_flag_defaults_closed_and_requires_storage_and_callbacks():
    configured=Settings(_env_file=None,V22_CAPABILITY_EXECUTION_ENABLED=False)
    assert not configured.V22_FIRST_SERVICE_DISCOVERY_ENABLED
    assert not v22_capability_execution_ready(configured)
    configured.V22_CAPABILITY_EXECUTION_ENABLED=True
    configured.V22_SUPABASE_URL="https://storage.example"
    configured.V22_SUPABASE_SERVICE_ROLE_KEY=SecretStr("test-key")
    configured.V22_CALLBACK_URL="https://app.example/callback"
    configured.V22_CALLBACK_SECRET=SecretStr("test-callback")
    assert v22_capability_execution_ready(configured)
    configured.V22_CALLBACK_SECRET=SecretStr("")
    assert not v22_capability_execution_ready(configured)


@pytest.mark.anyio
async def test_poll_and_sse_publish_only_the_versioned_reference_and_new_intake_is_not_implicitly_open(monkeypatch):
    from test_api_v2_jobs import build_app,AUTH_HEADERS
    from app.report_v22.capability import CapabilityReport
    from app.jobs_v22.models import utc_now
    m,_,_,task,raw=graph(completed=True)
    report=CapabilityReport.model_validate_json(json.dumps(raw["completed_report"]))
    app,runtime,store,queue=build_app(monkeypatch)
    now=utc_now()
    envelope=CapabilityRequestEnvelope(schema_version="v22_capability_request_envelope_v1",capability_request=task)
    await store.register_job(job_id=m.job_id,case_id=m.case_id,idempotency_key="frozen-analysis",request_payload=envelope.model_dump(mode="json"),now=now)
    await store.transition(m.job_id,status="running",stage="collecting_site",progress=1,message="Running",now=now)
    await store.transition(m.job_id,status="succeeded",stage="completed",progress=100,message="Analysis complete.",report=report,now=now+timedelta(seconds=1))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as client:
        assert (await client.get(f"/api/v2/tasks/{m.job_id}")).status_code==401
        status=await client.get(f"/api/v2/tasks/{m.job_id}",headers=AUTH_HEADERS)
        assert status.status_code==200
        assert status.json()["report_reference"]["report_id"]==str(m.job_id)
        streamed=await client.get(f"/api/v2/tasks/{m.job_id}/stream",headers=AUTH_HEADERS)
        assert streamed.status_code==200 and "report_reference" in streamed.text
        assert "input_manifest" not in streamed.text and '"report":' not in streamed.text
        rejected=await client.post("/api/v2/analyze",headers=AUTH_HEADERS,json=envelope.model_dump(mode="json"))
        assert rejected.status_code in (400,422) and queue.calls==[]
    await runtime.redis.aclose()


def test_separate_runtime_artifacts_are_deterministic():
    from pathlib import Path
    from scripts.export_v22_capability_runtime_contracts import artifacts
    root=Path(__file__).resolve().parents[1]/"contracts/v2.2-capability-runtime"
    assert {f.relative_to(root).as_posix():f.read_bytes() for f in root.rglob("*") if f.is_file()}==artifacts()
