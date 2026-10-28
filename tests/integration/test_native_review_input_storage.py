"""Disposable PostgreSQL binding to a reserved task, without live dispatch/payment."""
import asyncio
import json
from uuid import uuid4

import httpx
import pytest

from app.jobs_v22.digest import canonical_json_bytes
from app.jobs_v22.errors import DeterministicJobError, TransientJobError
from app.jobs_v22.review_input_repository import ReviewInputRepository, require_frozen_review_input
from app.jobs_v22.review_generation_stage import CheckpointedReviewGenerationStage
from app.jobs_v22.checkpoints import JobCheckpoints
from app.report_v22.review_generation_inputs import ReviewGenerationPreparer
from tests.integration.test_native_review_decisions import decisions, save
from tests.integration.test_native_review_freeze import review_setup, prepared, native_database, inspect
from tests.test_v22_review_decisions import request, item

pytestmark = [pytest.mark.native_integration, pytest.mark.anyio, pytest.mark.timeout(70)]


async def reserve_for_test(p, *, charged=True):
    """Test setup only, not a production intake. All statements commit together."""
    job = uuid4()
    async with await __import__("psycopg").AsyncConnection.connect(p["db"].url) as c:
        await c.execute("""insert into analysis_jobs(id,case_id,job_type,status,current_stage,idempotency_key,deadline_at)
          values(%s,%s,'verified_report','queued','queued',%s,clock_timestamp()+interval '20 minutes')""",
          [job, p["owner"]["case_id"], str(job)])
        if charged:
            cur = await c.execute("update users set credit_balance=credit_balance-1 where id=%s returning credit_balance", [p["owner"]["user_id"]])
            balance = (await cur.fetchone())[0]
            await c.execute("""insert into workflow_charges(id,user_id,case_id,workflow_kind,idempotency_key,analysis_job_id)
              values(%s,%s,%s,'verified',%s,%s)""", [job,p["owner"]["user_id"],p["owner"]["case_id"],str(job),job])
            await c.execute("""insert into credit_ledger(user_id,case_id,workflow_charge_id,analysis_job_id,kind,delta,balance_after,idempotency_key)
              values(%s,%s,%s,%s,'verified_debit',-1,%s,%s)""",
              [p["owner"]["user_id"],p["owner"]["case_id"],job,job,balance,f"workflow:{job}:debit"])
    return job


@pytest.fixture
async def bound_setup(decisions):
    p = decisions
    p["decision"] = await save(p, request(p["evidence"]))
    p["review_job"] = await reserve_for_test(p)
    p["preparer"] = ReviewGenerationPreparer(p["decision_service"])
    p["choice"] = dict(job_id=str(p["review_job"]),review_id=str(p["evidence"].id),
        review_checksum=p["evidence"].bundle.checksum,decision_id=str(p["decision"].id),decision_checksum=p["decision"].checksum)
    p["input"] = await p["preparer"].prepare(**p["owner"],selection=p["choice"])
    p["resolve_args"] = dict(user_id=p["input"].user_id,case_id=p["input"].case_id,job_id=p["review_job"],run_generation=1)
    p["reserved_baseline"] = await inspect(p["db"],p["scenario"])
    async with httpx.AsyncClient(transport=httpx.MockTransport(p["db"].transport)) as client:
        p["input_repo"] = ReviewInputRepository(url="https://local-test.invalid",service_role_key="local",http_client=client)
        yield p


async def freeze(p):
    return await p["input_repo"].freeze(value=p["input"],run_generation=1)


def raw_args(p):
    return {"p_"+k:str(v) if k!="run_generation" else v for k,v in p["resolve_args"].items()} | {
        "p_input_text":canonical_json_bytes(p["input"].model_dump(mode="json")).decode()}


async def test_native_binding_concurrency_exact_old_decision_and_no_additional_debit(bound_setup, arq_pool, redis_prefix):
    p=bound_setup
    newer=await save(p,request(p["evidence"],[item(p["evidence"],"adjust")],
        previous_id=str(p["decision"].id),previous_checksum=p["decision"].checksum))
    results=await asyncio.gather(*(freeze(p) for _ in range(4)))
    assert all(r.value==p["input"] and r.frozen_at==results[0].frozen_at for r in results)
    read=await p["input_repo"].resolve(**p["resolve_args"],expected_checksum=p["input"].checksum)
    assert require_frozen_review_input(read,run_generation=1)==p["input"]
    assert read.value.decision.id!=newer.id
    assert len(await p["db"].query("select job_id from capability_review_generation_inputs"))==1
    assert await inspect(p["db"],p["scenario"])==p["reserved_baseline"]
    checkpoints=JobCheckpoints(arq_pool,prefix=redis_prefix,ttl_seconds=600,run_generation=1)
    await arq_pool.set(checkpoints.keys.state(p["review_job"]),canonical_json_bytes(dict(run_generation=1,status="running")))
    content=await CheckpointedReviewGenerationStage().build_frozen(job_id=p["review_job"],frozen=read,run_generation=1,checkpoints=checkpoints)
    assert content.decision_id==p["decision"].id and content.delivery_state=="requires_delivery_review"
    assert await inspect(p["db"],p["scenario"])==p["reserved_baseline"]


async def test_native_binding_conflict_and_lost_ack_recovery(bound_setup):
    p=bound_setup; lost=False
    async def transport(req):
        nonlocal lost
        response=await p["db"].transport(req)
        if req.url.path.endswith("freeze_v22_review_generation_input") and response.status_code==200 and not lost:
            lost=True; raise httpx.ReadTimeout("lost private result",request=req)
        return response
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        repo=ReviewInputRepository(url="https://local-test.invalid",service_role_key="local",http_client=client)
        with pytest.raises(TransientJobError): await repo.freeze(value=p["input"],run_generation=1)
        recovered=await repo.resolve(**p["resolve_args"],expected_checksum=p["input"].checksum)
        assert recovered.value==p["input"]
        assert (await repo.freeze(value=p["input"],run_generation=1)).frozen_at==recovered.frozen_at
    later=await p["preparer"].prepare(**p["owner"],selection=p["choice"])
    assert later.checksum!=p["input"].checksum
    with pytest.raises(DeterministicJobError,match="CONFLICT"): await p["input_repo"].freeze(value=later,run_generation=1)
    assert await inspect(p["db"],p["scenario"])==p["reserved_baseline"]


async def test_native_binding_generation_takeover_and_deadline(bound_setup):
    p=bound_setup; await freeze(p)
    await p["db"].query("update analysis_jobs set run_generation=2 where id=%s",[p["review_job"]])
    with pytest.raises(DeterministicJobError,match="INACTIVE"): await freeze(p)
    with pytest.raises(DeterministicJobError,match="INACTIVE"): await p["input_repo"].resolve(**p["resolve_args"])
    current=await p["input_repo"].resolve(**(p["resolve_args"]|dict(run_generation=2)))
    assert require_frozen_review_input(current,run_generation=2)==p["input"]
    with pytest.raises(DeterministicJobError): require_frozen_review_input(current,run_generation=1)
    await p["db"].query("update analysis_jobs set created_at=clock_timestamp()-interval '1 minute',deadline_at=clock_timestamp()-interval '1 second' where id=%s",[p["review_job"]])
    with pytest.raises(DeterministicJobError,match="INACTIVE"):
        await p["input_repo"].resolve(**(p["resolve_args"]|dict(run_generation=2)))


async def test_native_binding_wrong_identity_archived_case_and_table_permissions(bound_setup):
    p=bound_setup; await freeze(p)
    for key in ("user_id","case_id","job_id"):
        with pytest.raises(DeterministicJobError): await p["input_repo"].resolve(**(p["resolve_args"]|{key:uuid4()}))
    for sql in ("update capability_review_generation_inputs set input_text=input_text where job_id=%s",
                "delete from capability_review_generation_inputs where job_id=%s"):
        with pytest.raises(Exception,match="IMMUTABLE"): await p["db"].query(sql,[p["review_job"]])
        with pytest.raises(Exception,match="permission denied"): await p["db"].query(sql,[p["review_job"]],True)
    rights=await p["db"].query("""select
      has_table_privilege('service_role','capability_review_generation_inputs','SELECT') a,
      has_table_privilege('authenticated','capability_review_generation_inputs','INSERT') b,
      has_function_privilege('anon','resolve_v22_review_generation_input(uuid,uuid,uuid,integer)','EXECUTE') c,
      has_function_privilege('service_role','v22_review_generation_job(uuid,uuid,uuid,integer)','EXECUTE') d""")
    assert not any(rights[0].values())
    await p["db"].query("update client_cases set status='archived',archived_at=clock_timestamp() where id=%s",[p["input"].case_id])
    with pytest.raises(DeterministicJobError,match="UNAVAILABLE"): await freeze(p)
    with pytest.raises(DeterministicJobError,match="UNAVAILABLE"): await p["input_repo"].resolve(**p["resolve_args"])


async def test_native_binding_missing_reservation_and_failed_task(bound_setup):
    p=bound_setup
    uncharged=await reserve_for_test(p,charged=False)
    value=await p["preparer"].prepare(**p["owner"],selection=p["choice"]|dict(job_id=str(uncharged)))
    with pytest.raises(DeterministicJobError,match="NOT_RESERVED"): await p["input_repo"].freeze(value=value,run_generation=1)
    await p["db"].query("update analysis_jobs set status='failed',completed_at=clock_timestamp(),error_code='TEST_FAILURE' where id=%s",[p["review_job"]])
    with pytest.raises(DeterministicJobError,match="INACTIVE"): await freeze(p)
    assert await p["db"].query("select job_id from capability_review_generation_inputs")==[]


async def test_native_binding_rejects_direct_rpc_tampering_before_storage(bound_setup):
    from app.jobs_v22.digest import verified_request_digest
    p=bound_setup; args=raw_args(p)
    for change in ("decision","evidence","checksum","extra","future","duplicate","oversize"):
        raw=p["input"].model_dump(mode="json")
        if change=="decision": raw["decision"]["decisions"][0]["reason"]="changed"
        elif change=="evidence": raw["evidence"]["bundle"]["delivery"]["summary"]="changed"
        elif change=="extra": raw["other"]=True
        elif change=="future": raw["prepared_at"]="2099-12-01T00:00:00Z"
        raw["checksum"]=verified_request_digest({k:v for k,v in raw.items() if k!="checksum"})
        if change=="checksum":raw["checksum"]="sha256:"+"0"*64
        body=canonical_json_bytes(raw).decode()
        if change=="duplicate":body=body[:-1]+',"checksum":"duplicate"}'
        if change=="oversize":body=" "*2300001
        with pytest.raises(Exception,match="REVIEW_GENERATION_(INVALID|TOO_LARGE)"):
            await p["db"].rpc("freeze_v22_review_generation_input",args|dict(p_input_text=body))
    assert await p["db"].query("select job_id from capability_review_generation_inputs")==[]
    assert await inspect(p["db"],p["scenario"])==p["reserved_baseline"]


async def test_native_binding_blocks_generic_success_and_opposing_inputs(bound_setup):
    p=bound_setup; await freeze(p)
    with pytest.raises(Exception,match="REVIEW_REPORT_COMPLETION_INVALID"):
        await p["db"].query("update analysis_jobs set status='succeeded',progress=100,completed_at=clock_timestamp() where id=%s",[p["review_job"]])
    for table in ("analysis_input_manifests","capability_first_service_preparations"):
        rows=await p["db"].query(f"select to_jsonb(t) value from {table} t where job_id=%s",[p["scenario"]["job"]])
        row=rows[0]["value"]|dict(job_id=str(p["review_job"]))
        if table=="analysis_input_manifests":row["id"]=str(uuid4())
        with pytest.raises(Exception,match="REVIEW_GENERATION_CONFLICT"):
            await p["db"].query(f"insert into {table} select * from jsonb_populate_record(null::{table},%s::jsonb)",[json.dumps(row)])
    # Before a review binding exists, a preparation from the ordinary path also
    # blocks enrollment. This uses an existing valid first-service source set.
    new_job=await reserve_for_test(p,charged=False)
    row=(await p["db"].query("select to_jsonb(t) value from capability_first_service_preparations t where job_id=%s",[p["scenario"]["job"]]))[0]["value"]
    row["job_id"]=str(new_job)
    await p["db"].query("insert into capability_first_service_preparations select * from jsonb_populate_record(null::capability_first_service_preparations,%s::jsonb)",[json.dumps(row)])
    value=await p["preparer"].prepare(**p["owner"],selection=p["choice"]|dict(job_id=str(new_job)))
    with pytest.raises(DeterministicJobError,match="CONFLICT"):await p["input_repo"].freeze(value=value,run_generation=1)
    assert len(await p["db"].query("select job_id from capability_review_generation_inputs"))==1


async def test_native_binding_refund_record_blocks_resolve_even_if_status_looks_active(bound_setup):
    p=bound_setup;await freeze(p)
    # Adversarial ledger state: an active-looking task cannot hide a recorded
    # credit return. Insert only in the isolated database, never live accounts.
    await p["db"].query("""insert into credit_ledger(user_id,case_id,workflow_charge_id,analysis_job_id,kind,delta,balance_after,idempotency_key)
      select %s,%s,%s,%s,'technical_failure_credit',1,credit_balance+1,%s from users where id=%s""",
      [p["input"].user_id,p["input"].case_id,p["review_job"],p["review_job"],str(uuid4()),p["input"].user_id])
    baseline=await inspect(p["db"],p["scenario"])
    with pytest.raises(DeterministicJobError,match="NOT_RESERVED"):await p["input_repo"].resolve(**p["resolve_args"])
    with pytest.raises(DeterministicJobError,match="NOT_RESERVED"):await freeze(p)
    assert await inspect(p["db"],p["scenario"])==baseline
