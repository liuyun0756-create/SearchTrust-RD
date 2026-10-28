from copy import deepcopy
import json
from uuid import uuid4

import httpx
import pytest

from app.jobs_v22.digest import verified_canonical_json_bytes, verified_request_digest
from app.jobs_v22.errors import DeterministicJobError, TransientJobError
from app.report_v22.page_check_storage import PageCheckJob, PageCheckSelection, PageCheckRepository
from test_v22_page_implementation_checks import prepared, observation
from test_v22_implementation_records import context


@pytest.fixture
def anyio_backend(): return "asyncio"


def choice(plan=None):
    plan=plan or prepared();b=plan.binding
    return PageCheckSelection.model_validate_json(json.dumps(dict(
        implementation=dict(id=str(b.implementation_id),revision=b.implementation_revision,checksum=b.implementation_checksum),
        action_id=b.action_id,target_url=plan.target_url,conditions=[c.model_dump(mode="json") for c in plan.conditions])))


def job_payload(status="queued", *, unable=False):
    p=prepared();cid=uuid4()
    payload=dict(schema_version="v22_page_check_job_v1",id=str(cid),user_id=str(p.binding.user_id),case_id=str(p.binding.case_id),
        report_id=str(p.binding.report_id),delivery_revision_id=str(p.binding.delivery_revision_id),
        selection=choice(p).model_dump(mode="json"),plan=p.model_dump(mode="json"),case_updated_at="2026-10-22T00:00:00Z",
        created_at="2026-10-23T00:59:00Z",status=status,lease_id=None,claimed_at=None,lease_expires_at=None,
        completed_at=None,error_code=None,observation=None)
    if status in ("running","completed"):
        payload.update(lease_id=str(uuid4()),claimed_at="2026-10-23T00:59:30Z",lease_expires_at="2026-10-23T01:01:30Z")
    if status=="failed": payload.update(completed_at="2026-10-23T01:00:01Z",error_code="PAGE_CHECK_EXPIRED")
    if status=="completed":
        o=observation(plan=p,failed=unable).model_dump(mode="json");o["id"]=str(cid)
        resign(o)
        payload.update(completed_at="2026-10-23T01:00:01Z",observation=o)
    return payload


def resign(payload):
    payload["checksum"]=verified_request_digest({k:v for k,v in payload.items() if k!="checksum"})
    return payload


def checked(payload): return PageCheckJob.model_validate_json(json.dumps(payload))


def resolved():
    record,s=context()
    return dict(record=record,report=s["report"].model_dump(mode="json"),
        report_checksum=verified_request_digest(s["report"].model_dump(mode="json")),
        business=s["report"].input_manifest.business.model_dump(mode="json"),delivery=s["delivery_content"].model_dump(mode="json"),
        delivery_id=str(s["delivery_revision_id"]),delivery_revision=s["delivery_revision"],case_updated_at="2026-10-22T00:00:00Z")


@pytest.mark.parametrize("status",["queued","running","completed","failed"])
def test_page_job_roundtrip(status):
    raw=job_payload(status)
    assert checked(raw).model_dump(mode="json")==raw
    if status=="completed": assert checked(job_payload(status,unable=True)).observation.status=="unable_to_check"


@pytest.mark.parametrize("field",["id","user_id","case_id","report_id","delivery_revision_id"])
def test_page_job_identity_mismatch(field):
    raw=job_payload("completed");raw[field]=str(uuid4())
    with pytest.raises(ValueError): checked(raw)


@pytest.mark.parametrize("change",[
    {"status":"completed"},{"lease_id":str(uuid4())},{"claimed_at":"2026-10-23T01:00:00Z"},
    {"completed_at":"2026-10-23T01:00:00Z"},{"error_code":"PAGE_CHECK_FAILED"},
    {"created_at":"2026-10-01T00:00:00Z"},{"case_updated_at":"2026-10-24T00:00:00Z"},
    {"observation":{}},{"unknown":True},
])
def test_page_job_rejects_unclaimed_state_confusion(change):
    with pytest.raises(ValueError): checked(job_payload()|change)


@pytest.mark.parametrize("change",[
    {"lease_id":None},{"claimed_at":None},{"lease_expires_at":None},
    {"claimed_at":"2026-10-23T00:58:00Z"},{"lease_expires_at":"2026-10-23T01:03:00Z"},
    {"completed_at":"2026-10-23T01:02:00Z"},{"completed_at":"2026-10-23T00:59:31Z"},
    {"status":"running"},{"status":"failed","error_code":"PAGE_CHECK_FAILED"},
    {"observation":None},{"error_code":"PRIVATE_TOKEN"},
])
def test_page_job_rejects_invalid_completion(change):
    with pytest.raises(ValueError): checked(job_payload("completed")|change)


@pytest.mark.parametrize("field",["implementation","action_id","target_url","conditions"])
def test_page_job_rejects_selection_rebinding(field):
    raw=job_payload()
    if field=="implementation": raw["selection"][field]["revision"]+=1
    elif field=="conditions": raw["selection"][field][0]["value"]="Different condition"
    elif field=="target_url": raw["selection"][field]+="-different"
    else: raw["selection"][field]="different-action"
    with pytest.raises(ValueError): checked(raw)


@pytest.mark.parametrize("which",["plan","observation"])
def test_page_job_digests_not_trusted(which):
    raw=job_payload("completed");raw[which]["checksum"]="sha256:"+"0"*64
    with pytest.raises(ValueError): checked(raw)


@pytest.mark.anyio
async def test_source_reconstruction_and_exact_owner_binding():
    p=prepared();calls=[]
    def handler(req): calls.append(req);return httpx.Response(200,json=resolved())
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        repo=PageCheckRepository(url="https://db.invalid",service_role_key="private-key",http_client=client)
        plan,marker,business=await repo.resolve(user_id=p.binding.user_id,case_id=p.binding.case_id,selection=choice(p))
    assert plan==p and json.loads(business)==resolved()["business"] and marker.endswith("Z")
    assert len(calls)==1 and calls[0].method=="POST"


@pytest.mark.anyio
@pytest.mark.parametrize("field",["record","report_checksum","business","delivery","delivery_id","delivery_revision","case_updated_at"])
async def test_resolver_rejects_tampered_sources(field):
    p=prepared();data=resolved()
    data[field]={} if isinstance(data[field],dict) else 2 if field=="delivery_revision" else str(uuid4())
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req:httpx.Response(200,json=data))) as client:
        repo=PageCheckRepository(url="https://db.invalid",service_role_key="private-key",http_client=client)
        with pytest.raises(DeterministicJobError,match="INVALID"):
            await repo.resolve(user_id=p.binding.user_id,case_id=p.binding.case_id,selection=choice(p))


@pytest.mark.anyio
async def test_repeated_request_reads_original_without_resolving_new_sources():
    raw=job_payload("completed");j=checked(raw);calls=[]
    def handler(req): calls.append(req.url.path);return httpx.Response(200,json=raw)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        repo=PageCheckRepository(url="https://db.invalid",service_role_key="private-key",http_client=client)
        assert await repo.request_check(user_id=j.user_id,case_id=j.case_id,check_id=j.id,selection=j.selection)==j
        with pytest.raises(DeterministicJobError,match="CONFLICT"):
            await repo.request_check(user_id=j.user_id,case_id=j.case_id,check_id=j.id,
                selection=j.selection.model_copy(update={"action_id":"other"}))
    assert calls==["/rest/v1/rpc/get_v22_page_check"]*2


@pytest.mark.anyio
@pytest.mark.parametrize("status",[301,400,401,403,429,500,503])
async def test_transport_safe_errors(status):
    p=prepared()
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req:httpx.Response(status,json={"secret":"private-key"}))) as client:
        repo=PageCheckRepository(url="https://db.invalid",service_role_key="private-key",http_client=client)
        error=TransientJobError if status==429 or status>=500 else DeterministicJobError
        with pytest.raises(error) as caught: await repo.read_check(user_id=p.binding.user_id,case_id=p.binding.case_id,check_id=uuid4())
    assert "private-key" not in str(caught.value)


@pytest.mark.anyio
@pytest.mark.parametrize("name,limit",[("get_v22_page_check",128*1024),("resolve_v22_page_check_input",2*1024*1024)])
async def test_transport_response_budget(name,limit):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req:httpx.Response(200,content=b" "*(limit+1)))) as client:
        repo=PageCheckRepository(url="https://db.invalid",service_role_key="key",http_client=client)
        with pytest.raises(DeterministicJobError,match="INVALID"): await repo._rpc(name,{})


@pytest.mark.anyio
@pytest.mark.parametrize("raw",[b"{}",b"[]",b"false",b"not-json",b"[1]",b"null"])
async def test_history_rejects_malformed_response(raw):
    p=prepared()
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req:httpx.Response(200,content=raw))) as client:
        repo=PageCheckRepository(url="https://db.invalid",service_role_key="key",http_client=client)
        if raw==b"[]": assert await repo.history(user_id=p.binding.user_id,case_id=p.binding.case_id)==[]
        else:
            with pytest.raises(DeterministicJobError): await repo.history(user_id=p.binding.user_id,case_id=p.binding.case_id)


@pytest.mark.anyio
async def test_forged_model_copy_rejected_before_transport():
    p=prepared();calls=[]
    def handler(req): calls.append(req);return httpx.Response(200,json=None)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        repo=PageCheckRepository(url="https://db.invalid",service_role_key="key",http_client=client)
        with pytest.raises(ValueError):
            await repo.request_check(user_id=p.binding.user_id,case_id=p.binding.case_id,check_id=uuid4(),
                selection=choice().model_copy(update={"target_url":"http://127.0.0.1/"}))
    assert calls==[]
