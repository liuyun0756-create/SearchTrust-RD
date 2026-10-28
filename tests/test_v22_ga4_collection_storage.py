"""Strict GA4 durable envelopes and execution gates; synthetic fixtures only."""
from copy import deepcopy
import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from app.jobs_v22.digest import request_digest, verified_request_digest
from app.jobs_v22.errors import DeterministicJobError
from app.report_v22.ga4_collection_storage import Ga4CollectionJob, Ga4CollectionRepository, Ga4CollectionSelection
from app.report_v22.ga4_collection_executor import execute_ga4_collection
from tests.support.ga4_comparison_fixtures import fixture, AT
from tests.support.comparison_fixtures import ga4_request_raw


@pytest.fixture
def anyio_backend(): return "asyncio"


def job_raw():
    collection=fixture();plan=collection.plan
    from tests.support.ga4_comparison_fixtures import record
    saved=record()
    return dict(schema_version="v22_ga4_collection_job_v1",id=str(uuid4()),user_id=str(saved.user_id),case_id=str(saved.case_id),
        connection_id=str(uuid4()),selection=dict(implementation=plan.implementation.model_dump(mode="json"),action_id=plan.action_id,
            days=28,binding_id=str(plan.binding_id),scope_snapshot_id=str(uuid4()),landing_path=None),record=saved.model_dump(mode="json"),
        plan=plan.model_dump(mode="json"),status="queued",lease_id=None,lease_expires_at=None,created_at=AT,
        completed_at=None,error_code=None,collection=None)


@pytest.mark.parametrize("change",["owner","case","record","selection","days","action","binding","path","plan",
    "waiting","collection","lease","running","succeeded","failed","completed","created","extra"])
def test_inconsistent_storage_reply_rejected(change):
    raw=job_raw();job=Ga4CollectionJob.model_validate_json(json.dumps(raw))
    if change=="owner":raw["user_id"]=str(uuid4())
    elif change=="case":raw["case_id"]=str(uuid4())
    elif change=="record":raw["record"]["revision"]=2
    elif change=="selection":raw["selection"]["implementation"]["checksum"]="sha256:"+"0"*64
    elif change=="days":raw["selection"]["days"]=7
    elif change=="action":raw["selection"]["action_id"]="unknown"
    elif change=="binding":raw["selection"]["binding_id"]=str(uuid4())
    elif change=="path":raw["selection"]["landing_path"]="/other"
    elif change=="plan":raw["plan"]["scope"]["hosts"]=["other.test"]
    elif change=="waiting":raw["plan"]["state"]="waiting"
    elif change=="collection":raw["collection"]={}
    elif change=="lease":raw["lease_id"]=str(uuid4())
    elif change in ("running","succeeded","failed"):raw["status"]=change
    elif change=="completed":raw["completed_at"]=AT
    elif change=="created":raw["created_at"]="2026-01-01T00:00:00Z"
    else:raw["access_token"]="secret"
    with pytest.raises(DeterministicJobError,match="V22_GA4_COLLECTION_INVALID"):
        Ga4CollectionRepository.checked(json.dumps(raw),collection_id=job.id,user_id=job.user_id,case_id=job.case_id)


@pytest.mark.parametrize("path",["https://example.test/a","//example.test/a","/a?x=y","/a#x","/a b","/a\n","/a\\b"])
def test_selection_refuses_urls_queries_and_control_characters(path):
    raw=job_raw()["selection"];raw["landing_path"]=path
    with pytest.raises(ValueError): Ga4CollectionSelection.model_validate_json(json.dumps(raw))


def test_resigned_collection_cannot_remove_qualification_or_backdate_persistence():
    raw=job_raw();collection=fixture().model_dump(mode="json")
    raw.update(status="succeeded",lease_id=str(uuid4()),lease_expires_at="2026-12-15T08:05:00Z",completed_at=AT,collection=collection)
    job=Ga4CollectionJob.model_validate_json(json.dumps(raw))
    collection["qualification"]["metrics"][0]["reasons"]=["FABRICATED"]
    collection["checksum"]=verified_request_digest({k:v for k,v in collection.items() if k!="checksum"})
    with pytest.raises(DeterministicJobError): Ga4CollectionRepository.checked(json.dumps(raw),collection_id=job.id)
    raw["collection"]=fixture().model_dump(mode="json");raw["completed_at"]="2026-12-15T07:59:59Z"
    with pytest.raises(DeterministicJobError): Ga4CollectionRepository.checked(json.dumps(raw),collection_id=job.id)


@pytest.mark.anyio
async def test_read_is_read_only_and_rejects_wrong_identity():
    raw=job_raw();job=Ga4CollectionJob.model_validate_json(json.dumps(raw));calls=[]
    def response(req): calls.append(req.url.path);return httpx.Response(200,json=raw)
    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        repo=Ga4CollectionRepository(url="https://storage.example",service_role_key="secret",http_client=client)
        assert await repo.read_collection(user_id=job.user_id,case_id=job.case_id,collection_id=job.id)==job
        with pytest.raises(DeterministicJobError): await repo.read_collection(user_id=job.user_id,case_id=job.case_id,collection_id=uuid4())
    assert calls==["/rest/v1/rpc/get_v22_ga4_collection"]*2


@pytest.mark.anyio
@pytest.mark.parametrize("kind",["missing","wrong_job","not_claimed","broker_error"])
async def test_executor_fails_closed_without_authorized_budget(kind):
    raw=job_raw();raw.update(status="running",lease_id=str(uuid4()),lease_expires_at="2026-12-15T08:05:00Z")
    job=Ga4CollectionJob.model_validate_json(json.dumps(raw));calls=[]
    class Repo:
        async def claim_collection(self,cid):return None if kind=="not_claimed" else job
        async def fail_collection(self,job,code):calls.append(code)
        async def read_collection(self,**kwargs):return "failed"
    class Broker:
        async def access_token(self,connection):calls.append("broker");raise RuntimeError("private secret")
    class Ledger:
        job_id=job.id
        async def ensure(self):calls.append("ledger")
    ledger=Ledger() if kind=="broker_error" else SimpleNamespace(job_id=uuid4()) if kind=="wrong_job" else None
    outcome=await execute_ga4_collection(Repo(),job.id,provider=None,token_broker=Broker(),ledger_factory=lambda _:ledger)
    assert outcome==(None if kind=="not_claimed" else "failed")
    assert calls==([] if kind=="not_claimed" else ["ledger","broker","SYNC_FAILED"] if kind=="broker_error" else ["SYNC_COST_LEDGER_REQUIRED"])


@pytest.mark.anyio
@pytest.mark.parametrize("change",["owner","source_id","source_case","source_binding","checksum","hosts","timezone","resource","path"])
async def test_resolver_inconsistency_never_reaches_persistence(change):
    raw=job_raw();job=Ga4CollectionJob.model_validate_json(json.dumps(raw));calls=[]
    source=deepcopy(ga4_request_raw()["after"])
    source.update(snapshot_id=str(job.selection.scope_snapshot_id),case_id=str(job.case_id),binding_id=str(job.selection.binding_id))
    resolved=dict(record=job.record.model_dump(mode="json"),scope_source=source,scope=job.plan.scope.model_dump(mode="json"),prepared_at=AT)
    if change=="owner": resolved["record"]["user_id"]=str(uuid4())
    elif change=="source_id": source["snapshot_id"]=str(uuid4())
    elif change=="source_case": source["case_id"]=str(uuid4())
    elif change=="source_binding": source["binding_id"]=str(uuid4())
    elif change=="checksum": source["payload_checksum"]="sha256:"+"0"*64
    elif change=="hosts": resolved["scope"]["hosts"]=["other.test"]
    elif change=="resource": resolved["scope"]["resource_id"]="properties/999"
    elif change=="path": resolved["scope"]["landing_path"]="/other"
    else:
        source["normalized_payload"]["previous"]["dates"]["metadata"]["time_zone"]="UTC"
        source["payload_checksum"]=request_digest(source["normalized_payload"])
    def handler(req):
        calls.append(req.url.path)
        return httpx.Response(200,content="null") if len(calls)==1 else httpx.Response(200,json=resolved)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        repo=Ga4CollectionRepository(url="https://storage.example",service_role_key="secret",http_client=client)
        with pytest.raises(DeterministicJobError,match="V22_GA4_COLLECTION_INVALID"):
            await repo.request_collection(user_id=job.user_id,case_id=job.case_id,collection_id=job.id,selection=job.selection)
    assert calls==["/rest/v1/rpc/get_v22_ga4_collection","/rest/v1/rpc/resolve_v22_ga4_collection_input"]
