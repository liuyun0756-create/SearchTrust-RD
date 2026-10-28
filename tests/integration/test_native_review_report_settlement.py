"""Formal review report + one reserved credit settle in one local transaction."""
import asyncio
from copy import deepcopy
from uuid import uuid4

import httpx
import pytest

from app.jobs_v22.digest import canonical_json_bytes, verified_request_digest
from app.jobs_v22.errors import DeterministicJobError, TransientJobError
from app.jobs_v22.review_report_persistence import ReviewReportRepository, ReviewCompletionService
from app.report_v22.review_report import build_verified_review_report
from tests.integration.test_native_review_input_storage import (
    bound_setup, decisions, review_setup, prepared, native_database, freeze, reserve_for_test,
)
from tests.integration.test_native_review_decisions import save
from tests.test_v22_review_decisions import request,item

pytestmark=[pytest.mark.native_integration,pytest.mark.anyio,pytest.mark.timeout(70)]


def repo(p,client=None):
    return ReviewReportRepository(url="https://local-test.invalid",service_role_key="local",http_client=client or p["input_repo"].client)


async def state(p):
    return dict(balance=(await p["db"].query("select credit_balance from users where id=%s",[p["input"].user_id]))[0]["credit_balance"],
        ledger=await p["db"].query("select kind,delta from credit_ledger where workflow_charge_id=%s order by kind",[p["review_job"]]),
        job=(await p["db"].query("select status,capability_report_id from analysis_jobs where id=%s",[p["review_job"]]))[0],
        charge=(await p["db"].query("select state,final_capability_report_id from workflow_charges where analysis_job_id=%s",[p["review_job"]]))[0])


async def choose(p,kind=None):
    d=await save(p,request(p["evidence"],[] if kind is None else [item(p["evidence"],kind,"new-work" if kind=="new" else None)],
        previous_id=str(p["decision"].id),previous_checksum=p["decision"].checksum))
    p["input"]=await p["preparer"].prepare(**p["owner"],selection=p["choice"]|dict(decision_id=str(d.id),decision_checksum=d.checksum))


@pytest.mark.parametrize("kind",["continue","adjust","observe","defer","new",None])
async def test_native_formal_review_settles_once_and_keeps_original_diagnosis(bound_setup,kind):
    p=bound_setup
    if kind!="continue":await choose(p,kind)
    frozen=await freeze(p);before=await state(p)
    original=await p["db"].query("select payload from capability_report_versions where id=%s",[p["input"].report_id])
    report,receipt=await ReviewCompletionService(repo(p)).complete(frozen=frozen,run_generation=1)
    assert receipt.idempotent is False
    after=await state(p)
    assert after["balance"]==before["balance"] and after["ledger"]==before["ledger"]==[dict(kind="verified_debit",delta=-1)]
    assert after["job"]["status"]=="succeeded" and after["charge"]["state"]=="consumed"
    assert after["charge"]["final_capability_report_id"]==report.id
    assert report.version==p["input"].evidence.bundle.report.version+1 and report.report_type=="verified_execution"
    assert report.content.prior_diagnosis.findings
    assert bool(report.actions)==(kind in {"continue","adjust","new"})
    assert await repo(p).read(**{k:v for k,v in p["resolve_args"].items() if k!="run_generation"})==report
    repeated=await repo(p).complete(frozen=frozen,report=report,run_generation=1)
    assert repeated.idempotent and await state(p)==after
    assert await p["db"].query("select payload from capability_report_versions where id=%s",[p["input"].report_id])==original
    # Existing legacy workspace listing never hands 2.2.3 to a 2.2.2 renderer.
    listed=await p["db"].query("select id from list_v22_completed_capability_reports(%s)",[p["input"].user_id],True)
    assert report.id not in [r["id"] for r in listed]


async def test_native_completion_concurrent_replay_lost_ack_and_late_recovery(bound_setup):
    p=bound_setup;frozen=await freeze(p);report=build_verified_review_report(p["input"]);lost=False
    async def transport(req):
        nonlocal lost
        response=await p["db"].transport(req)
        if req.url.path.endswith("complete_v22_review_analysis") and response.status_code==200 and not lost:
            lost=True;raise httpx.ReadTimeout("lost private completion",request=req)
        return response
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        with pytest.raises(TransientJobError):await repo(p,client).complete(frozen=frozen,report=report,run_generation=1)
        assert await repo(p,client).read(**{k:v for k,v in p["resolve_args"].items() if k!="run_generation"})==report
    results=await asyncio.gather(*(repo(p).complete(frozen=frozen,report=report,run_generation=1) for _ in range(4)))
    assert all(r.idempotent for r in results)
    await p["db"].query("update analysis_jobs set created_at=clock_timestamp()-interval '1 hour',deadline_at=clock_timestamp()-interval '1 minute' where id=%s",[p["review_job"]])
    assert (await repo(p).complete(frozen=frozen,report=report,run_generation=1)).idempotent
    assert (await state(p))["ledger"]==[dict(kind="verified_debit",delta=-1)]


async def test_native_tampered_reports_roll_back_without_settlement(bound_setup):
    p=bound_setup;await freeze(p);report=build_verified_review_report(p["input"]);before=await state(p)
    args={"p_"+k:str(v) if k!="run_generation" else v for k,v in p["resolve_args"].items()}
    for kind in ("id","version","recipe","actions","private_client","cause","diagnosis"):
        raw=report.model_dump(mode="json")
        if kind=="id":raw["id"]=str(uuid4())
        elif kind=="version":raw["version"]+=1
        elif kind=="recipe":raw["input_reference"]["decision_id"]=str(uuid4())
        elif kind=="actions":raw["actions"]=[];raw["action_state"]="no_next_actions"
        elif kind=="private_client":raw["content"]["client_actions"][0]["next_decision"]["text"]="PRIVATE SEO NOTE"
        elif kind=="cause":raw["content"]["causal_attribution"]=True
        else:raw["content"]["prior_diagnosis"]["summary"]="Altered original diagnosis"
        raw["content"]["checksum"]=verified_request_digest({k:v for k,v in raw["content"].items() if k!="checksum"})
        raw["checksum"]=verified_request_digest({k:v for k,v in raw.items() if k!="checksum"})
        with pytest.raises(Exception,match="REVIEW_REPORT_INVALID"):
            await p["db"].rpc("complete_v22_review_analysis",args|dict(p_report_text=canonical_json_bytes(raw).decode()))
        assert await state(p)==before
    assert await p["db"].query("select id from capability_report_versions where id=%s",[p["review_job"]])==[]


async def test_native_inactive_generation_owner_and_delivery_guards(bound_setup):
    p=bound_setup;frozen=await freeze(p);report=build_verified_review_report(p["input"])
    args={"p_"+k:str(v) if k!="run_generation" else v for k,v in p["resolve_args"].items()}|dict(p_report_text=report.model_dump_json())
    for key in ("p_user_id","p_case_id","p_job_id"):
        with pytest.raises(Exception,match="REVIEW_REPORT_UNAVAILABLE"):await p["db"].rpc("complete_v22_review_analysis",args|{key:str(uuid4())})
    with pytest.raises(Exception,match="REVIEW_REPORT_INACTIVE"):await p["db"].rpc("complete_v22_review_analysis",args|dict(p_run_generation=2))
    await repo(p).complete(frozen=frozen,report=report,run_generation=1)
    content=dict(schema_version="v22_delivery_revision_v1",report_id=str(report.id),user_id=str(p["input"].user_id),case_id=str(p["input"].case_id),
        expected_revision=0,source_checksum=verified_request_digest(report.model_dump(mode="json")),status="approved",title=report.title,summary=report.summary,client_note=None,
        actions=[a.model_dump(mode="json") for a in report.actions])
    with pytest.raises(Exception,match="REVIEW_DELIVERY_NOT_READY"):
        await p["db"].query("select save_v22_completed_report_revision(%s,%s,%s,%s::jsonb)",
            [p["input"].user_id,p["input"].case_id,report.id,canonical_json_bytes(content).decode()],True)


async def test_native_mid_transaction_failure_rolls_back_report_and_consumption(bound_setup):
    p=bound_setup;frozen=await freeze(p);report=build_verified_review_report(p["input"]);before=await state(p)
    await p["db"].query("""create function reject_test_review_completion() returns trigger language plpgsql as $$
      begin raise exception 'TEST_COMPLETION_FAILURE'; end; $$""")
    await p["db"].query("create trigger reject_test_review_completion before update on analysis_jobs for each row execute function reject_test_review_completion()")
    with pytest.raises(DeterministicJobError):await repo(p).complete(frozen=frozen,report=report,run_generation=1)
    assert await state(p)==before
    assert await p["db"].query("select id from capability_report_versions where id=%s",[p["review_job"]])==[]
    await p["db"].query("drop trigger reject_test_review_completion on analysis_jobs")
    assert (await repo(p).complete(frozen=frozen,report=report,run_generation=1)).idempotent is False
    assert (await state(p))["ledger"]==before["ledger"]


async def test_native_expired_active_task_and_old_generation_cannot_settle(bound_setup):
    p=bound_setup;frozen=await freeze(p);report=build_verified_review_report(p["input"])
    await p["db"].query("update analysis_jobs set run_generation=2 where id=%s",[p["review_job"]])
    with pytest.raises(DeterministicJobError,match="INACTIVE"):await repo(p).complete(frozen=frozen,report=report,run_generation=1)
    fresh=await p["input_repo"].resolve(**(p["resolve_args"]|dict(run_generation=2)))
    await p["db"].query("update analysis_jobs set created_at=clock_timestamp()-interval '1 hour',deadline_at=clock_timestamp()-interval '1 minute' where id=%s",[p["review_job"]])
    before=await state(p)
    with pytest.raises(DeterministicJobError,match="INACTIVE"):await repo(p).complete(frozen=fresh,report=report,run_generation=2)
    assert await state(p)==before and before["charge"]["state"]=="reserved"
