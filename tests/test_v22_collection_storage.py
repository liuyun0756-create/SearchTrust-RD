"""Strict durable envelope and execution boundary; synthetic material only."""
import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from app.jobs_v22.errors import DeterministicJobError
from app.report_v22.collection_storage import CollectionJob, CollectionRepository
from app.report_v22.collection_executor import execute_collection
from app.report_v22.comparison_collection import prepare_gsc_comparison
from test_v22_comparison_collection import record, options, AT

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend(): return "asyncio"


def job_raw():
    saved=record();plan=prepare_gsc_comparison(saved,**options())
    return dict(schema_version="v22_collection_job_v1",id=str(uuid4()),user_id=str(saved.user_id),case_id=str(saved.case_id),
        connection_id=str(uuid4()),selection=dict(implementation=plan.implementation.model_dump(mode="json"),
            action_id=plan.action_id,days=28,binding_id=str(plan.binding_id)),record=saved.model_dump(mode="json"),
        plan=plan.model_dump(mode="json"),status="queued",lease_id=None,lease_expires_at=None,created_at=AT,
        completed_at=None,error_code=None,collection=None)


@pytest.mark.parametrize("change",["owner","case","record","selection","days","action","binding","plan",
    "waiting","collection","lease","running","succeeded","failed","completed","created","extra"])
async def test_inconsistent_storage_reply_rejected(change):
    raw=job_raw();original=CollectionJob.model_validate_json(json.dumps(raw))
    if change=="owner":raw["user_id"]=str(uuid4())
    elif change=="case":raw["case_id"]=str(uuid4())
    elif change=="record":raw["record"]["revision"]=2
    elif change=="selection":raw["selection"]["implementation"]["checksum"]="sha256:"+"0"*64
    elif change=="days":raw["selection"]["days"]=7
    elif change=="action":raw["selection"]["action_id"]="unknown"
    elif change=="binding":raw["selection"]["binding_id"]=str(uuid4())
    elif change=="plan":raw["plan"]["windows"]["after"]["end"]="2026-12-01"
    elif change=="waiting":raw["plan"]["state"]="waiting"
    elif change=="collection":raw["collection"]={}
    elif change=="lease":raw["lease_id"]=str(uuid4())
    elif change in ("running","succeeded","failed"):raw["status"]=change
    elif change=="completed":raw["completed_at"]=AT
    elif change=="created":raw["created_at"]="2026-01-01T00:00:00Z"
    else:raw["access_token"]="secret"
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _:httpx.Response(200,json=raw))) as client:
        repo=CollectionRepository(url="https://storage.example",service_role_key="secret",http_client=client)
        with pytest.raises(DeterministicJobError,match="V22_COLLECTION_INVALID"):
            await repo.read_collection(user_id=original.user_id,case_id=original.case_id,collection_id=original.id)


async def test_read_is_read_only_and_rejects_wrong_id():
    raw=job_raw();job=CollectionJob.model_validate_json(json.dumps(raw));calls=[]
    def response(req):
        calls.append(req.url.path);return httpx.Response(200,json=raw)
    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        repo=CollectionRepository(url="https://storage.example",service_role_key="secret",http_client=client)
        assert await repo.read_collection(user_id=job.user_id,case_id=job.case_id,collection_id=job.id)==job
        with pytest.raises(DeterministicJobError):
            await repo.read_collection(user_id=job.user_id,case_id=job.case_id,collection_id=uuid4())
    assert calls==["/rest/v1/rpc/get_v22_collection"]*2


@pytest.mark.parametrize("kind",["missing","wrong_job","not_claimed","broker_error"])
async def test_executor_fails_closed_without_authorized_budget(kind):
    raw=job_raw();raw.update(status="running",lease_id=str(uuid4()),lease_expires_at="2026-12-15T08:05:00Z")
    job=CollectionJob.model_validate_json(json.dumps(raw));calls=[]
    class Repo:
        async def claim_collection(self, cid):return None if kind=="not_claimed" else job
        async def fail_collection(self, job, code):calls.append(code)
        async def read_collection(self, **kwargs):return "failed"
    class Broker:
        async def access_token(self, connection):
            calls.append("broker");raise RuntimeError("private secret")
    class Ledger:
        job_id=job.id
        async def ensure(self):calls.append("ledger")
    ledger=Ledger() if kind=="broker_error" else SimpleNamespace(job_id=uuid4()) if kind=="wrong_job" else None
    outcome=await execute_collection(Repo(),job.id,provider=None,token_broker=Broker(),ledger_factory=lambda _:ledger)
    assert outcome== (None if kind=="not_claimed" else "failed")
    assert calls==([] if kind=="not_claimed" else ["ledger","broker","SYNC_FAILED"] if kind=="broker_error" else ["SYNC_COST_LEDGER_REQUIRED"])


async def test_request_rejects_resolved_other_owner_before_persistence():
    raw=job_raw();job=CollectionJob.model_validate_json(json.dumps(raw));calls=[]
    def handler(req):
        calls.append(req.url.path)
        return httpx.Response(200,content="null") if len(calls)==1 else httpx.Response(200,json=dict(record=job.record.model_dump(mode="json")))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        repo=CollectionRepository(url="https://storage.example",service_role_key="secret",http_client=client)
        with pytest.raises(DeterministicJobError,match="V22_COLLECTION_INVALID"):
            await repo.request_collection(user_id=uuid4(),case_id=job.case_id,collection_id=job.id,selection=job.selection)
    assert calls==["/rest/v1/rpc/get_v22_collection","/rest/v1/rpc/resolve_v22_collection_input"]
