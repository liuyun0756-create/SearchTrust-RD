"""Disposable native SQL, mocked public HTTP/DNS, no live websites or accounts."""
import asyncio
from copy import deepcopy
from datetime import timedelta
import json
from uuid import uuid4

import httpx
import pytest

from app.collectors.page_implementation import collect_page_implementation
from app.collectors.site_inventory_fetcher import AsyncRateLimiter
from app.jobs_v22.digest import verified_canonical_json_bytes, verified_request_digest
from app.jobs_v22.errors import DeterministicJobError, TransientJobError
from app.jobs_v22.models import utc_now
from app.report_v22.page_check_executor import execute_page_check
from app.report_v22.page_check_storage import PageCheckRepository, PageCheckSelection
from tests.integration.test_native_comparison_storage import prepared, native_database, inspect

pytestmark = [pytest.mark.native_integration, pytest.mark.anyio, pytest.mark.timeout(50)]


@pytest.fixture
async def page_setup(prepared, monkeypatch):
    p = prepared
    async def no_wait(self):
        pass
    monkeypatch.setattr(AsyncRateLimiter, "wait", no_wait)
    report = p["baseline"]["reports"][0]["payload"]
    site = report["input_manifest"]["business"]["site_url"]
    p["choice"] = PageCheckSelection.model_validate_json(json.dumps(dict(
        implementation=p["selection"].implementation.model_dump(mode="json"), action_id=p["selection"].action_id,
        target_url=site+"service", conditions=[dict(id="service",kind="text_present",value="Repair service")])) )
    p["calls"] = []
    async def resolver(host):
        return ["93.184.216.34"]
    p["resolver"] = resolver
    def handler(req):
        p["calls"].append(req)
        return httpx.Response(200, content=b"User-agent: *\nAllow: /" if req.url.path == "/robots.txt" else b"<h1>Repair service</h1>",
            headers={"content-type":"text/html"})
    p["handler"] = handler
    async with httpx.AsyncClient(transport=httpx.MockTransport(p["db"].transport)) as client:
        p["pages"] = PageCheckRepository(url="https://local-test.invalid",service_role_key="local",http_client=client)
        p["ordinary"] = await unaffected(p)
        yield p


async def unaffected(p):
    return [await p["db"].query(f"select * from {table} where case_id=%s order by id",[p["identity"]["case_id"]])
        for table in ("data_snapshots","case_source_bindings","google_sync_jobs","report_content_revisions","capability_implementation_records")]


async def request(p, *, check_id=None, selection=None):
    return await p["pages"].request_check(**p["identity"],check_id=check_id or uuid4(),selection=selection or p["choice"])


async def raw_request(p, *, check_id=None, plan_change=None, selection=None, marker=None):
    selection=selection or p["choice"]
    plan,version,business=await p["pages"].resolve(**p["identity"],selection=selection)
    body,canonical=signed(plan.model_dump(mode="json")|(plan_change or {}))
    return await p["db"].rpc("request_v22_page_check",dict(p_user_id=str(p["identity"]["user_id"]),p_case_id=str(p["identity"]["case_id"]),
        p_id=str(check_id or uuid4()),p_selection=selection.model_dump(mode="json"),p_plan=body,p_case_updated_at=marker or version,
        p_canonical_plan=canonical,p_canonical_business=business))


async def execute(p, job, handler=None):
    return await execute_page_check(p["pages"],job.id,resolver=p["resolver"],
        client_factory=lambda target:httpx.AsyncClient(transport=httpx.MockTransport(handler or p["handler"])))


async def material(p, job):
    return await collect_page_implementation(job.plan,observation_id=job.id,resolver=p["resolver"],
        client_factory=lambda target:httpx.AsyncClient(transport=httpx.MockTransport(p["handler"])))


def signed(value):
    value=deepcopy(value); value.pop("checksum",None)
    canonical=verified_canonical_json_bytes(value).decode()
    value["checksum"]=verified_request_digest(value)
    return value,canonical


async def revise(p):
    value=deepcopy(p["record"]);value.update(revision=2,recorded_at=utc_now().isoformat().replace("+00:00","Z"))
    value["entries"][0]["implemented_on"]=(utc_now()-timedelta(days=45)).date().isoformat()
    value,canonical=signed(value)
    await p["db"].query("select save_v22_implementation_record(%s,%s,%s,%s,1,%s::jsonb,%s)",
        [p["identity"]["user_id"],p["identity"]["case_id"],value["report_id"],value["delivery_revision_id"],json.dumps(value),canonical],True)


async def expire_fixture(p, job):
    # Test-admin-only time travel. Production roles cannot update tables or disable triggers.
    await p["db"].query("""alter table capability_page_check_jobs disable trigger immutable_page_check""")
    try:
        await p["db"].query("""update capability_page_check_jobs set created_at=created_at-interval '11 minutes',
          claimed_at=claimed_at-interval '11 minutes',lease_expires_at=lease_expires_at-interval '11 minutes' where id=%s""",[job.id])
    finally:
        await p["db"].query("alter table capability_page_check_jobs enable trigger immutable_page_check")


async def test_native_page_concurrent_request_claim_and_no_source_or_credit_writes(page_setup):
    p=page_setup; cid=uuid4()
    jobs=await asyncio.gather(*(request(p,check_id=cid) for _ in range(5)))
    assert all(j==jobs[0] for j in jobs)
    with pytest.raises(DeterministicJobError): await request(p)
    with pytest.raises(DeterministicJobError,match="CONFLICT"):
        await request(p,check_id=cid,selection=p["choice"].model_copy(update={"target_url":p["choice"].target_url+"-other"}))
    attempts=await asyncio.gather(*(execute(p,jobs[0]) for _ in range(4)))
    saved=[j for j in attempts if j is not None]
    assert len(saved)==1 and saved[0].status=="completed" and saved[0].observation.status=="checked"
    assert len(p["calls"])==2 and await request(p,check_id=cid)==saved[0]
    assert await execute(p,saved[0]) is None
    assert len(await p["pages"].history(**p["identity"]))==1
    assert await unaffected(p)==p["ordinary"]
    assert await inspect(p["db"],p["scenario"])==p["baseline"]


@pytest.mark.parametrize("kind",["robots","http","timeout"])
async def test_native_page_unable_is_completed_not_negative_or_retried(page_setup,kind):
    p=page_setup;job=await request(p)
    def handler(req):
        p["calls"].append(req)
        if kind=="timeout": raise httpx.ReadTimeout("private timeout detail",request=req)
        if kind=="robots": return httpx.Response(200,content=b"User-agent: *\nDisallow: /")
        return httpx.Response(404)
    saved=await execute(p,job,handler)
    assert saved.status=="completed" and saved.observation.status=="unable_to_check"
    assert all(r.state=="unable_to_check" for r in saved.observation.results)
    assert "private timeout detail" not in saved.model_dump_json()
    calls=len(p["calls"]);assert await execute(p,job) is None and len(p["calls"])==calls


@pytest.mark.parametrize("boundary",["claim","finish","history"])
async def test_native_page_revision_invalidates_pending_not_historical(page_setup,boundary):
    p=page_setup;job=await request(p)
    if boundary!="claim":
        claimed=await p["pages"].claim_check(job.id);observation=await material(p,claimed)
    if boundary=="history": saved=await p["pages"].finish_check(claimed,observation)
    await revise(p)
    if boundary=="claim": assert await execute(p,job) is None and not p["calls"]
    elif boundary=="finish": assert (await p["pages"].finish_check(claimed,observation)).status=="failed"
    recovered=await request(p,check_id=job.id)
    assert recovered==saved if boundary=="history" else recovered.error_code=="PAGE_CHECK_SOURCE_CHANGED"
    with pytest.raises(DeterministicJobError): await request(p)


@pytest.mark.parametrize("boundary",["claim","finish"])
@pytest.mark.parametrize("change",["case","business","access"])
async def test_native_page_source_changes_fence_work(page_setup,boundary,change):
    p=page_setup;job=await request(p)
    if boundary=="finish": claimed=await p["pages"].claim_check(job.id);o=await material(p,claimed)
    if change=="case": await p["db"].query("update client_cases set updated_at=clock_timestamp() where id=%s",[job.case_id])
    elif change=="business":
        await p["db"].query("""insert into case_business_scope_versions(id,user_id,case_id,revision,scope)
          select %s,user_id,case_id,revision+1,scope from case_business_scope_versions where case_id=%s order by revision desc limit 1""",[uuid4(),job.case_id])
    else: await p["db"].query("update client_cases set status='archived',archived_at=clock_timestamp() where id=%s",[job.case_id])
    if boundary=="claim": assert await execute(p,job) is None and not p["calls"]
    else: assert (await p["pages"].finish_check(claimed,o)).status=="failed"
    if change=="access":
        with pytest.raises(DeterministicJobError): await request(p,check_id=job.id)
    else: assert (await request(p,check_id=job.id)).status=="failed"
    assert await p["db"].query("select id from capability_page_observations")==[]


@pytest.mark.parametrize("operation",["request_v22_page_check","claim_v22_page_check","finish_v22_page_check"])
async def test_native_page_lost_ack_never_refetches(page_setup,operation):
    p=page_setup;cid=uuid4();lost=False
    async def transport(req):
        nonlocal lost
        answer=await p["db"].transport(req)
        if req.url.path.endswith("/"+operation) and answer.status_code==200 and not lost:
            lost=True;raise httpx.ReadTimeout("ack lost",request=req)
        return answer
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        p["pages"]=PageCheckRepository(url="https://local-test.invalid",service_role_key="local",http_client=client)
        if operation=="request_v22_page_check":
            with pytest.raises(TransientJobError): await request(p,check_id=cid)
            job=await request(p,check_id=cid);assert not p["calls"]
            assert (await execute(p,job)).status=="completed"
        else:
            job=await request(p,check_id=cid)
            with pytest.raises(TransientJobError): await execute(p,job)
            recovered=await request(p,check_id=cid)
            assert recovered.status==("running" if operation=="claim_v22_page_check" else "completed")
            assert len(p["calls"])==(0 if operation=="claim_v22_page_check" else 2)
            assert await execute(p,job) is None
            if operation=="claim_v22_page_check":
                await expire_fixture(p,job);assert await p["pages"].expire_check(job.id)
                assert (await p["db"].query("select error_code from capability_page_check_jobs where id=%s",[job.id]))[0]["error_code"]=="PAGE_CHECK_INTERRUPTED"


@pytest.mark.parametrize("state",["queued","cancelled","failed"])
async def test_native_page_expiry_cancel_and_executor_failure(page_setup,state,monkeypatch):
    p=page_setup;job=await request(p)
    if state=="cancelled":
        started=asyncio.Event()
        async def wait(req):
            p["calls"].append(req);started.set();await asyncio.Event().wait()
        task=asyncio.create_task(execute(p,job,wait));await asyncio.wait_for(started.wait(),5);task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        assert (await request(p,check_id=job.id)).status=="running"
    if state=="failed":
        async def boom(*args,**kwargs): raise RuntimeError("private collector fault")
        monkeypatch.setattr("app.report_v22.page_check_executor.collect_page_implementation",boom)
        saved=await execute(p,job)
        assert saved.status=="failed" and saved.error_code=="PAGE_CHECK_FAILED" and "private" not in saved.model_dump_json()
    else:
        await expire_fixture(p,job)
        before=await p["db"].query("select * from capability_page_check_jobs where id=%s",[job.id])
        # Raw read/history cannot advance expired state. Test time travel invalidates model source time only.
        await p["db"].rpc("get_v22_page_check",dict(p_user_id=str(job.user_id),p_case_id=str(job.case_id),p_id=str(job.id)))
        await p["db"].rpc("list_v22_page_checks",dict(p_user_id=str(job.user_id),p_case_id=str(job.case_id)))
        assert await p["db"].query("select * from capability_page_check_jobs where id=%s",[job.id])==before
        assert await p["pages"].expire_check(job.id)
        assert not await p["pages"].expire_check(job.id)
        assert await execute(p,job) is None
    assert await p["db"].query("select id from capability_page_observations")==[]


async def test_native_page_save_replay_tamper_privileges_and_immutability(page_setup):
    p=page_setup;job=await request(p);claimed=await p["pages"].claim_check(job.id);o=await material(p,claimed)
    for change in ({"id":str(uuid4())},{"observed_at":"2099-01-01T00:00:00Z"},{"observed_at":job.created_at},
                   {"status":"unknown"},{"effect_conclusion":"effective"},{"causal_eligible":True},
                   {"results":[{"condition_id":"wrong","state":"observed"}]},{"response_bytes":False},
                   {"http_status":"200"},{"failure":"http_status"},{"media_type":None},{"content_checksum":None},
                   {"plan":{}},{"extra":"forbidden"}):
        payload,canonical=signed(o.model_dump(mode="json")|change)
        with pytest.raises(Exception,match="PAGE_CHECK_INVALID"):
            await p["db"].rpc("finish_v22_page_check",dict(p_id=str(job.id),p_lease_id=str(claimed.lease_id),p_payload=payload,p_canonical_payload=canonical))
    saved=await p["pages"].finish_check(claimed,o)
    assert await p["pages"].finish_check(claimed,o)==saved
    payload,canonical=signed(o.model_dump(mode="json")|{"response_bytes":100})
    with pytest.raises(Exception,match="CONFLICT"):
        await p["db"].rpc("finish_v22_page_check",dict(p_id=str(job.id),p_lease_id=str(claimed.lease_id),p_payload=payload,p_canonical_payload=canonical))
    for table in ("capability_page_check_jobs","capability_page_observations"):
        for statement in (f"update {table} set id=id where id=%s",f"delete from {table} where id=%s"):
            with pytest.raises(Exception,match="IMMUTABLE"): await p["db"].query(statement,[job.id])
    rights=await p["db"].query("""select
      has_table_privilege('service_role','capability_page_check_jobs','UPDATE') a,
      has_table_privilege('service_role','capability_page_observations','INSERT') b,
      has_table_privilege('authenticated','capability_page_check_jobs','SELECT') c,
      has_function_privilege('anon','get_v22_page_check(uuid,uuid,uuid)','EXECUTE') d,
      has_function_privilege('authenticated','claim_v22_page_check(uuid)','EXECUTE') e,
      has_function_privilege('service_role','v22_lock_page_check(uuid)','EXECUTE') f""")
    assert not any(rights[0].values())
    with pytest.raises(DeterministicJobError):
        await p["pages"].read_check(user_id=uuid4(),case_id=job.case_id,check_id=job.id)
    assert await inspect(p["db"],p["scenario"])==p["baseline"]


async def test_native_page_source_selection_and_signed_plan_rejected_before_fetch(page_setup):
    p=page_setup
    for change in ({"action_id":"unknown"},{"implementation":p["choice"].implementation.model_copy(update={"revision":2})},
                   {"target_url":"https://foreign.test/service"}):
        with pytest.raises(DeterministicJobError): await request(p,selection=p["choice"].model_copy(update=change))
    for change in ({"rules_version":"new-rules"},{"target_url":p["choice"].target_url+"changed"},
                   {"conditions":[{"id":"service","kind":"text_present","value":"other"}]},{"binding":{}},{"extra":True}):
        with pytest.raises(Exception,match="PAGE_CHECK_INVALID"): await raw_request(p,plan_change=change)
    with pytest.raises(Exception,match="SOURCE_CHANGED"): await raw_request(p,marker="2000-01-01T00:00:00Z")
    assert not p["calls"] and await p["db"].query("select id from capability_page_check_jobs")==[]


async def test_native_page_unapproved_delivery_has_no_authority(page_setup):
    p=page_setup
    # Fixture-admin corruption of an approval simulates missing entitlement; source checks still reject.
    await p["db"].query("alter table report_content_revisions disable trigger user")
    try:
        await p["db"].query("update report_content_revisions set status='draft',approved_at=null where id=%s",[p["record"]["delivery_revision_id"]])
    finally:
        await p["db"].query("alter table report_content_revisions enable trigger user")
    with pytest.raises(DeterministicJobError): await request(p)
    assert not p["calls"]


async def test_native_page_new_draft_keeps_exact_approved_delivery_usable(page_setup):
    p=page_setup;job=await request(p)
    delivery=(await p["db"].query("select content from report_content_revisions where id=%s",[job.delivery_revision_id]))[0]["content"]
    delivery.update(expected_revision=1,status="draft",client_note="New draft; approved source unchanged")
    await p["db"].query("select * from save_v22_completed_report_revision(%s,%s,%s,%s::jsonb)",
        [job.user_id,job.case_id,job.report_id,json.dumps(delivery)],True)
    assert (await execute(p,job)).status=="completed"


async def test_native_page_latest_is_per_delivery_not_record_root(page_setup):
    p=page_setup;job=await request(p)
    # A different root cannot bypass latest-per-delivery validation, even in legacy/imported data.
    r=deepcopy(p["record"]);r.update(id=str(uuid4()),revision=2,recorded_at=utc_now().isoformat().replace("+00:00","Z"));r,_=signed(r)
    await p["db"].query("""insert into capability_implementation_records(id,revision,user_id,case_id,report_id,delivery_revision_id,payload,checksum,recorded_at)
      values(%s,2,%s,%s,%s,%s,%s::jsonb,%s,%s)""",
      [r["id"],job.user_id,job.case_id,job.report_id,job.delivery_revision_id,json.dumps(r),r["checksum"],r["recorded_at"]])
    assert await execute(p,job) is None and not p["calls"]
    assert (await request(p,check_id=job.id)).error_code=="PAGE_CHECK_SOURCE_CHANGED"


@pytest.mark.parametrize("zone",["Asia/Shanghai","America/Los_Angeles","Pacific/Kiritimati"])
async def test_native_page_non_utc_database_time_is_normalized(page_setup,zone):
    from psycopg import AsyncConnection
    from psycopg.rows import dict_row
    p=page_setup;job=await request(p)
    async with await AsyncConnection.connect(p["db"].url,row_factory=dict_row) as connection:
        await connection.execute("select set_config('TimeZone',%s,true)",[zone])
        await connection.execute("set local role service_role")
        row=await (await connection.execute("select claim_v22_page_check(%s) value",[job.id])).fetchone()
    claimed=p["pages"].checked(json.dumps(row["value"]),check_id=job.id)
    assert claimed.claimed_at.endswith("Z")
    o=await material(p,claimed)
    async with await AsyncConnection.connect(p["db"].url,row_factory=dict_row) as connection:
        await connection.execute("select set_config('TimeZone',%s,true)",[zone])
        await connection.execute("set local role service_role")
        row=await (await connection.execute("select finish_v22_page_check(%s,%s,%s::jsonb,%s) value",
            [job.id,claimed.lease_id,o.model_dump_json(),verified_canonical_json_bytes(o.model_dump(mode="json",exclude={"checksum"})).decode()])).fetchone()
    saved=p["pages"].checked(json.dumps(row["value"]),check_id=job.id)
    assert saved.status=="completed" and saved.completed_at.endswith("Z")


async def test_native_page_finish_waits_for_implementation_revision_lock(page_setup):
    from psycopg import AsyncConnection
    p=page_setup;job=await request(p);claimed=await p["pages"].claim_check(job.id);o=await material(p,claimed)
    r=deepcopy(p["record"]);r.update(revision=2,recorded_at=utc_now().isoformat().replace("+00:00","Z"))
    r["entries"][0]["implemented_on"]=(utc_now()-timedelta(days=45)).date().isoformat();r,canonical=signed(r)
    async with await AsyncConnection.connect(p["db"].url) as connection:
        await connection.execute("set local role service_role")
        await connection.execute("select save_v22_implementation_record(%s,%s,%s,%s,1,%s::jsonb,%s)",
            [job.user_id,job.case_id,job.report_id,job.delivery_revision_id,json.dumps(r),canonical])
        finishing=asyncio.create_task(p["pages"].finish_check(claimed,o))
        await asyncio.sleep(.05)
        assert not finishing.done()
        await connection.commit()
        saved=await asyncio.wait_for(finishing,5)
    assert saved.status=="failed" and saved.error_code=="PAGE_CHECK_SOURCE_CHANGED"
    assert await p["db"].query("select id from capability_page_observations")==[]


async def test_native_page_save_retry_without_repeat_get_and_wrong_lease_fenced(page_setup):
    p=page_setup;job=await request(p);claimed=await p["pages"].claim_check(job.id);o=await material(p,claimed);lost=False
    async def transport(req):
        nonlocal lost
        if req.url.path.endswith("/finish_v22_page_check") and not lost:
            lost=True;raise httpx.ReadTimeout("before commit",request=req)
        return await p["db"].transport(req)
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        repo=PageCheckRepository(url="https://local-test.invalid",service_role_key="local",http_client=client)
        with pytest.raises(TransientJobError): await repo.finish_check(claimed,o)
        assert (await request(p,check_id=job.id)).status=="running"
        saved=await repo.finish_check(claimed,o)
        assert saved.status=="completed" and len(p["calls"])==2
        with pytest.raises(DeterministicJobError): await repo.finish_check(claimed.model_copy(update={"lease_id":uuid4()}),o)


async def test_native_page_history_bounded_and_owner_collision_is_private(page_setup):
    p=page_setup
    for index in range(22):
        job=await request(p);claimed=await p["pages"].claim_check(job.id);await p["pages"].fail_check(claimed)
        if index < 21:
            # Test-admin time travel, preserving the production rate guard. Only
            # summary reads follow for these aged historical fixture rows.
            await p["db"].query("alter table capability_page_check_jobs disable trigger immutable_page_check")
            try:
                await p["db"].query("update capability_page_check_jobs set created_at=created_at-interval '25 hours' where status='failed'")
            finally:
                await p["db"].query("alter table capability_page_check_jobs enable trigger immutable_page_check")
    history=await p["pages"].history(**p["identity"])
    assert len(history)==20 and history[0].id==job.id
    assert not p["calls"]
    other_case=uuid4()
    await p["db"].query("""insert into client_cases(id,user_id,site_url,normalized_domain,business_name,business_identity,operating_model,primary_service,target_market)
      select %s,user_id,site_url,normalized_domain,business_name,business_identity-'primary_location',operating_model,primary_service,target_market
      from client_cases where id=%s""",[other_case,job.case_id])
    assert await p["pages"].read_check(user_id=job.user_id,case_id=other_case,check_id=job.id) is None
    with pytest.raises(Exception,match="PAGE_CHECK_UNAVAILABLE"):
        await p["db"].rpc("request_v22_page_check",dict(p_user_id=str(job.user_id),p_case_id=str(other_case),p_id=str(job.id),
            p_selection={},p_plan={},p_case_updated_at=utc_now().isoformat(),p_canonical_plan="{}",p_canonical_business="{}"))
