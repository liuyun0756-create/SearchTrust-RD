"""Owner/API/dispatch boundaries for the separate GA4 entry, no live providers."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4
import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr
from app.api.v2.ga4_collections import router, collection_service
from app.core.config import settings
from app.jobs_v22.errors import DeterministicJobError, TransientJobError
from app.report_v22.ga4_collection_entry import Ga4CollectionCommand, Ga4CollectionEntry
from app.report_v22.ga4_collection_storage import Ga4CollectionJob
from app.report_v22.ga4_collection_worker import reconcile_v22_ga4_collections, execute_v22_ga4_collection
from test_v22_ga4_collection_storage import job_raw

pytestmark = pytest.mark.anyio
@pytest.fixture
def anyio_backend(): return "asyncio"
def command(job, operation="context"):
    return Ga4CollectionCommand(operation=operation,user_id=job.user_id,case_id=job.case_id,
        report_id=job.record.report_id,delivery_revision_id=job.record.delivery_revision_id,
        collection_id=None if operation=="context" else job.id,selection=job.selection if operation=="request" else None)
def setup():
    job=Ga4CollectionJob.model_validate_json(json.dumps(job_raw()))
    data=dict(record=job.record.model_dump(mode="json"),candidates=[dict(binding_id=str(job.selection.binding_id),
        scope_snapshot_id=str(job.selection.scope_snapshot_id),observed_at=job.created_at)],job_ids=[str(job.id)])
    repo=SimpleNamespace(_rpc=AsyncMock(return_value=json.dumps(data)),read_collection=AsyncMock(return_value=job),
        resolve_plan=AsyncMock(return_value=job.plan),request_collection=AsyncMock(return_value=job))
    return job,data,repo

@pytest.mark.parametrize("change",["valid","null","wrong_owner","extra","duplicate_binding","duplicate_job","missing_job","wrong_job_report"])
async def test_context_projects_only_fully_revalidated_metadata(change):
    job,data,repo=setup()
    if change=="null":data=None
    elif change=="wrong_owner":data["record"]["user_id"]=str(uuid4())
    elif change=="extra":data["access_token"]="secret"
    elif change=="duplicate_binding":data["candidates"]*=2
    elif change=="duplicate_job":data["job_ids"]*=2
    elif change=="missing_job":repo.read_collection.return_value=None
    elif change=="wrong_job_report":job.record.report_id=uuid4()
    repo._rpc.return_value=json.dumps(data);request=command(Ga4CollectionJob.model_validate_json(json.dumps(job_raw()))) if change=="wrong_job_report" else command(job)
    if change!="valid":
        with pytest.raises((DeterministicJobError,ValueError)):await Ga4CollectionEntry(repo).run(request)
    else:
        result=await Ga4CollectionEntry(repo).run(request)
        assert len(result.previews)==4*len(job.record.entries) and len(result.jobs)==1
        assert result.bindings[0].scope==job.plan.scope
        assert "normalized_payload" not in result.model_dump_json() and "user_id" not in result.model_dump_json()
    repo.request_collection.assert_not_called()

@pytest.mark.parametrize("change",["recover","wrong_report","selection","missing"])
async def test_recovery_uses_original_id_without_new_provider_request(change):
    job,_,repo=setup();request=command(job,"read" if change=="missing" else "request")
    if change=="wrong_report":request.report_id=uuid4()
    elif change=="selection":request.selection=request.selection.model_copy(update={"days":7})
    elif change=="missing":repo.read_collection.return_value=None
    if change=="recover":assert (await Ga4CollectionEntry(repo).run(request)).id==job.id
    else:
        with pytest.raises(DeterministicJobError):await Ga4CollectionEntry(repo).run(request)
    repo.request_collection.assert_not_called();repo._rpc.assert_not_called()

@pytest.mark.parametrize("kind,status",[("valid",200),("no_auth",401),("bad_auth",401),
    ("V22_GA4_COMPARISON_COLLECTIONS_ENABLED",404),("V22_COMPARISONS_ENABLED",404),("V22_CAPABILITY_EXECUTION_ENABLED",404),
    ("wrong_type",415),("large",413),("malformed",400),("extra",400),("bad_shape",400),
    ("not_found",404),("conflict",409),("waiting",409),("transient",503),("private_error",503)])
async def test_api_gates_and_safe_errors(monkeypatch,kind,status):
    job,_,_=setup();request=command(job);view=Ga4CollectionEntry(None).view(job,request)
    for flag in ("V22_GA4_COMPARISON_COLLECTIONS_ENABLED","V22_COMPARISONS_ENABLED","V22_CAPABILITY_EXECUTION_ENABLED"):
        monkeypatch.setattr(settings,flag,kind!=flag)
    monkeypatch.setattr(settings,"V22_INTERNAL_API_TOKEN",SecretStr("test-only"))
    service=SimpleNamespace(run=AsyncMock(return_value=view))
    codes={"not_found":"V22_GA4_COLLECTION_NOT_FOUND","conflict":"V22_GA4_COLLECTION_CONFLICT","waiting":"V22_GA4_COLLECTION_WAITING","private_error":"PRIVATE_PROVIDER_CODE"}
    if kind in codes:service.run.side_effect=DeterministicJobError(codes[kind],"private provider text")
    if kind=="transient":service.run.side_effect=TransientJobError("PRIVATE","private provider text")
    app=FastAPI();app.include_router(router);app.dependency_overrides[collection_service]=lambda:service
    data=request.model_dump(mode="json")
    if kind=="extra":data["access_token"]="private"
    if kind=="bad_shape":data["collection_id"]=str(job.id)
    raw="x"*20001 if kind=="large" else "{" if kind=="malformed" else json.dumps(data)
    headers={"Content-Type":"text/plain" if kind=="wrong_type" else "application/json","Authorization":"Bearer wrong" if kind=="bad_auth" else "Bearer test-only"}
    if kind=="no_auth":headers.pop("Authorization")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://local") as client:
        response=await client.post("/api/v2/ga4-comparison-collections",content=raw,headers=headers)
    assert response.status_code==status
    assert "private provider text" not in response.text and "PRIVATE_PROVIDER_CODE" not in response.text
    if status==200:assert response.headers["cache-control"]=="private, no-store"
    if kind not in {"valid",*codes,"transient"}:service.run.assert_not_called()

@pytest.mark.parametrize("pending",[[],[str(uuid4())],["invalid"],[str(uuid4())]*21,{},None])
async def test_dispatch_bounded_existing_ids_only(pending):
    rpc=AsyncMock(return_value=json.dumps(pending));enqueue=AsyncMock()
    await reconcile_v22_ga4_collections(dict(ga4_comparison_collection_repository=SimpleNamespace(_rpc=rpc),
        redis=SimpleNamespace(enqueue_job=enqueue),ga4_comparison_collection_queue="test-queue"))
    if isinstance(pending,list) and len(pending)==1 and pending[0]!="invalid":
        enqueue.assert_awaited_once_with("execute_v22_ga4_collection",pending[0],_job_id=f"ga4-comparison-collection:{pending[0]}",_queue_name="test-queue")
    else:enqueue.assert_not_called()
    rpc.assert_awaited_once_with("pending_v22_ga4_collections",{})

async def test_disabled_worker_needs_no_connection():
    await reconcile_v22_ga4_collections({})
    assert await execute_v22_ga4_collection({},str(uuid4())) is None

async def test_worker_registers_single_attempt_without_redis_result():
    from app.jobs_v22.worker import WorkerSettings
    functions=[f for f in WorkerSettings.functions if f.name=="execute_v22_ga4_collection"]
    assert len(functions)==1 and functions[0].max_tries==1 and functions[0].timeout_s==315 and functions[0].keep_result_s==0
    crons=[c for c in WorkerSettings.cron_jobs if c.name=="reconcile_v22_ga4_collections"]
    assert len(crons)==1 and crons[0].unique and crons[0].max_tries==1

@pytest.mark.parametrize("disabled",[None,"V22_GA4_COMPARISON_COLLECTIONS_ENABLED","V22_COMPARISONS_ENABLED","V22_CAPABILITY_EXECUTION_ENABLED"])
async def test_worker_dependency_gate_and_ga4_broker(monkeypatch,disabled):
    import fakeredis.aioredis
    from app.jobs_v22.worker import on_startup,on_shutdown
    from app.google_connections_v22.ga4_comparison import Ga4ComparisonProvider
    for flag in ("V22_GA4_COMPARISON_COLLECTIONS_ENABLED","V22_COMPARISONS_ENABLED","V22_CAPABILITY_EXECUTION_ENABLED"):
        monkeypatch.setattr(settings,flag,flag!=disabled)
    monkeypatch.setattr(settings,"V22_SUPABASE_URL","https://storage.test")
    monkeypatch.setattr(settings,"V22_SUPABASE_SERVICE_ROLE_KEY",SecretStr("test-only"))
    monkeypatch.setattr(settings,"V22_GOOGLE_BROKER_ORIGIN","https://broker.test")
    monkeypatch.setattr(settings,"V22_GOOGLE_BROKER_SECRET",SecretStr("test-only-"*4))
    redis=fakeredis.aioredis.FakeRedis(decode_responses=False);ctx={"redis":redis}
    try:
        await on_startup(ctx)
        if disabled:assert "ga4_comparison_collection_repository" not in ctx
        else:
            assert isinstance(ctx["ga4_comparison_collection_provider"],Ga4ComparisonProvider)
            assert ctx["ga4_comparison_collection_broker"].source=="ga4"
    finally:
        await on_shutdown(ctx);await redis.aclose()
    if not disabled:assert ctx["ga4_comparison_collection_client"].is_closed
