"""Collection owner boundary and dispatch safeguards, without network access."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr

from app.api.v2.collections import router, collection_service
from app.core.config import settings
from app.jobs_v22.errors import DeterministicJobError, TransientJobError
from app.report_v22.collection_entry import CollectionCommand, CollectionEntry
from app.report_v22.collection_storage import CollectionJob
from app.report_v22.collection_worker import reconcile_v22_collections, execute_v22_collection
from test_v22_collection_storage import job_raw

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend(): return "asyncio"


def command(job, operation="context"):
    return CollectionCommand(operation=operation, user_id=job.user_id, case_id=job.case_id,
        report_id=job.record.report_id, delivery_revision_id=job.record.delivery_revision_id,
        collection_id=None if operation=="context" else job.id,
        selection=job.selection if operation=="request" else None)


def setup_context():
    job=CollectionJob.model_validate_json(json.dumps(job_raw()))
    data=dict(implementation=job.selection.implementation.model_dump(mode="json"),
        implementation_record=job.record.model_dump(mode="json"),
        bindings=[dict(id=str(job.selection.binding_id),resource=job.plan.resource_id)],jobs=[])
    return job,data


@pytest.mark.parametrize("change",["valid","null","list","missing_record","missing_ref","wrong_owner","wrong_ref","extra_secret"])
async def test_context_projects_only_bound_metadata(change):
    job,data=setup_context()
    if change=="null":data=None
    elif change=="list":data=[]
    elif change=="missing_record":data.pop("implementation_record")
    elif change=="missing_ref":data["implementation"]=None
    elif change=="wrong_owner":data["implementation_record"]["user_id"]=str(uuid4())
    elif change=="wrong_ref":data["implementation"]["revision"]+=1
    elif change=="extra_secret":data["access_token"]="private material"
    rpc=AsyncMock(return_value=json.dumps(data))
    entry=CollectionEntry(SimpleNamespace(_rpc=rpc))
    if change!="valid":
        with pytest.raises(DeterministicJobError):await entry.run(command(job))
    else:
        result=await entry.run(command(job))
        assert len(result.previews)==4*len(job.record.entries)
        assert all(p.windows.timezone=="America/Los_Angeles" for p in result.previews)
        assert "implementation_record" not in result.model_dump_json()
        assert "snapshot" not in result.model_dump_json()
    assert rpc.await_count==1
    assert rpc.call_args.args[0]=="get_v22_collection_context"


@pytest.mark.parametrize("kind",["recover","wrong_report","changed_selection","read_missing"])
async def test_recover_never_starts_another_request(kind):
    job,_=setup_context();request=command(job,"read" if kind=="read_missing" else "request")
    if kind=="wrong_report":request.report_id=uuid4()
    if kind=="changed_selection":request.selection=request.selection.model_copy(update={"days":7})
    repo=SimpleNamespace(read_collection=AsyncMock(return_value=None if kind=="read_missing" else job),
        request_collection=AsyncMock(),_rpc=AsyncMock())
    if kind=="recover":assert (await CollectionEntry(repo).run(request)).id==job.id
    else:
        with pytest.raises(DeterministicJobError):await CollectionEntry(repo).run(request)
    repo.request_collection.assert_not_called();repo._rpc.assert_not_called()


@pytest.mark.parametrize("kind,status",[("valid",200),("no_auth",401),("bad_auth",401),
    ("V22_COMPARISON_COLLECTIONS_ENABLED",404),("V22_COMPARISONS_ENABLED",404),("V22_CAPABILITY_EXECUTION_ENABLED",404),
    ("wrong_type",415),("large",413),("malformed",400),("extra",400),("bad_shape",400),
    ("not_found",404),("conflict",409),("waiting",409),("transient",503),("private_error",503)])
async def test_api_gates_and_safe_errors(monkeypatch,kind,status):
    job,_=setup_context();request=command(job);view=CollectionEntry(None).view(job,request)
    for flag in ("V22_COMPARISON_COLLECTIONS_ENABLED","V22_COMPARISONS_ENABLED","V22_CAPABILITY_EXECUTION_ENABLED"):
        monkeypatch.setattr(settings,flag,kind!=flag)
    monkeypatch.setattr(settings,"V22_INTERNAL_API_TOKEN",SecretStr("test-only"))
    service=SimpleNamespace(run=AsyncMock(return_value=view))
    codes={"not_found":"V22_COLLECTION_NOT_FOUND","conflict":"V22_COLLECTION_CONFLICT", "waiting":"V22_COLLECTION_WAITING","private_error":"PRIVATE_PROVIDER_CODE"}
    if kind in codes:service.run.side_effect=DeterministicJobError(codes[kind],"private provider text")
    if kind=="transient":service.run.side_effect=TransientJobError("PRIVATE","private provider text")
    app=FastAPI();app.include_router(router);app.dependency_overrides[collection_service]=lambda:service
    data=request.model_dump(mode="json")
    if kind=="extra":data["access_token"]="private"
    if kind=="bad_shape":data["collection_id"]=str(job.id)
    raw="x"*20001 if kind=="large" else "{" if kind=="malformed" else json.dumps(data)
    headers={"Content-Type":"text/plain" if kind=="wrong_type" else "application/json", "Authorization":"Bearer wrong" if kind=="bad_auth" else "Bearer test-only"}
    if kind=="no_auth":headers.pop("Authorization")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://local") as client:
        response=await client.post("/api/v2/comparison-collections",content=raw,headers=headers)
    assert response.status_code==status
    assert "private provider text" not in response.text and "PRIVATE_PROVIDER_CODE" not in response.text
    if status==200:assert response.headers["cache-control"]=="private, no-store"
    if kind not in {"valid",*codes,"transient"}:service.run.assert_not_called()


@pytest.mark.parametrize("pending",[[],[str(uuid4())],["invalid"],[str(uuid4())]*21,{},None])
async def test_dispatch_is_bounded_and_only_enqueues_existing_ids(pending):
    rpc=AsyncMock(return_value=json.dumps(pending));enqueue=AsyncMock()
    await reconcile_v22_collections(dict(comparison_collection_repository=SimpleNamespace(_rpc=rpc),
        redis=SimpleNamespace(enqueue_job=enqueue),comparison_collection_queue="test-queue"))
    if isinstance(pending,list) and len(pending)==1 and pending[0]!="invalid":
        enqueue.assert_awaited_once_with("execute_v22_collection",pending[0],
            _job_id=f"comparison-collection:{pending[0]}",_queue_name="test-queue")
    else:enqueue.assert_not_called()
    rpc.assert_awaited_once_with("pending_v22_collections",{})


async def test_disabled_worker_does_not_need_connections():
    await reconcile_v22_collections({})
    assert await execute_v22_collection({},str(uuid4())) is None
