"""Formal report assembly and completion transport; no live account operations."""
from copy import deepcopy
from uuid import uuid4

import httpx
import pytest

from app.jobs_v22.digest import verified_request_digest
from app.jobs_v22.errors import DeterministicJobError,TransientJobError
from app.jobs_v22.review_report_persistence import ReviewReportRepository,ReviewCompletionService
from app.jobs_v22.review_input_repository import ReviewInputRepository
from app.report_v22.review_report import build_verified_review_report,validate_verified_review_report
from tests.test_v22_review_generation import generation,resign
from tests.test_v22_review_decisions import example,item,zero_evidence
from tests.test_v22_review_input_repository import receipt


@pytest.fixture
def anyio_backend():return "asyncio"


@pytest.mark.parametrize("kind",["continue","adjust","observe","defer","new",None])
def test_formal_work_plan_and_private_client_boundaries(kind):
    saved=example()[0];d=item(saved,kind,"new-work" if kind=="new" else None) if kind else None
    if d:d["reason"]="PRIVATE_REASON"
    value=generation(saved=saved,decisions=[d] if d else [])
    report=build_verified_review_report(value)
    assert validate_verified_review_report(report,value)==report
    assert report.schema_version=="2.2.3" and report.report_type=="verified_execution"
    assert len(report.actions)==(1 if kind in {"continue","adjust","new"} else 0)
    assert report.content.prior_diagnosis.findings and report.settlement_policy=="one_credit_per_completed_analysis"
    assert report.generated_at==value.prepared_at
    assert all(a.next_decision.system_completion_verified is False for a in report.content.client_actions)
    assert "PRIVATE_REASON" not in str([a.model_dump(mode="json") for a in report.content.client_actions])
    if d:assert "PRIVATE_REASON" in report.content.model_dump_json()


def test_zero_original_actions_and_long_work_remain_lossless():
    report=build_verified_review_report(generation(saved=zero_evidence(),decisions=[]))
    assert report.actions==[] and report.content.prior_diagnosis.action_state=="insufficient_basis"
    assert report.content.prior_diagnosis.findings
    saved=example()[0];d=item(saved,"adjust");d["work"]=" "*2000+"Keep the complete work description."
    report=build_verified_review_report(generation(saved=saved,decisions=[d]))
    assert report.actions[0].steps==[d["work"]]


@pytest.mark.parametrize("field",["facts","private","version","checksum","action","cause"])
def test_resigned_forgery_is_not_a_report(field):
    value=generation();raw=build_verified_review_report(value).model_dump(mode="json")
    if field=="facts":raw["content"]["client_actions"][0]["facts"]["title"]="changed"
    elif field=="private":raw["content"]["client_actions"][0]["next_decision"]["text"]="PRIVATE_NOTE"
    elif field=="version":raw["version"]+=1
    elif field=="action":raw["actions"][0]["steps"]=["Unselected work"]
    elif field=="cause":raw["content"]["causal_attribution"]=True
    raw["content"]=resign(raw["content"]);raw=resign(raw)
    if field=="checksum":raw["checksum"]="sha256:"+"0"*64
    with pytest.raises(DeterministicJobError):validate_verified_review_report(raw,value)


async def frozen(value):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _:httpx.Response(200,json=receipt(value)))) as c:
        return await ReviewInputRepository(url="https://local.invalid",service_role_key="local",http_client=c).freeze(value=value,run_generation=1)


@pytest.mark.anyio
async def test_formal_completion_and_read_replay_all_facts():
    value=generation();report=build_verified_review_report(value);saved=await frozen(value);calls=[]
    payload_hash=verified_request_digest(report.model_dump(mode="json"))
    def handler(req):
        calls.append(req)
        if req.url.path.endswith("complete_v22_review_analysis"):
            return httpx.Response(200,json=dict(report_id=str(value.job_id),payload_checksum=payload_hash,idempotent=False))
        return httpx.Response(200,json=dict(schema_version="v22_completed_review_report_v1",report=report.model_dump(mode="json"),
            payload_checksum=payload_hash,input_text=value.model_dump_json()))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        repo=ReviewReportRepository(url="https://local.invalid",service_role_key="local",http_client=c)
        output,ack=await ReviewCompletionService(repo).complete(frozen=saved,run_generation=1)
        assert output==report and ack.idempotent is False
        assert await repo.read(user_id=value.user_id,case_id=value.case_id,job_id=value.job_id)==report
    assert len(calls)==2


@pytest.mark.anyio
@pytest.mark.parametrize("change",["id","hash","extra","bool","invalid","duplicate"])
async def test_bad_success_receipt_remains_unknown(change):
    value=generation();saved=await frozen(value);report=build_verified_review_report(value)
    ack=dict(report_id=str(value.job_id),payload_checksum=verified_request_digest(report.model_dump(mode="json")),idempotent=False)
    if change=="id":ack["report_id"]=str(uuid4())
    elif change=="hash":ack["payload_checksum"]="sha256:"+"0"*64
    elif change=="extra":ack["private"]=True
    elif change=="bool":ack["idempotent"]=1
    import json
    text=json.dumps(ack)
    if change=="invalid":text="not json"
    elif change=="duplicate":text=text[:-1]+',"idempotent":true}'
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _:httpx.Response(200,content=text))) as c:
        repo=ReviewReportRepository(url="https://local.invalid",service_role_key="local",http_client=c)
        with pytest.raises(TransientJobError,match="STORAGE_UNAVAILABLE"):await repo.complete(frozen=saved,report=report,run_generation=1)


@pytest.mark.anyio
async def test_altered_report_never_reaches_completion_transport():
    value=generation();saved=await frozen(value);report=build_verified_review_report(value)
    report.actions[0].steps=["unreviewed mutation"]
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _:pytest.fail("Invalid report reached storage"))) as c:
        repo=ReviewReportRepository(url="https://local.invalid",service_role_key="local",http_client=c)
        with pytest.raises(DeterministicJobError):await repo.complete(frozen=saved,report=report,run_generation=1)
