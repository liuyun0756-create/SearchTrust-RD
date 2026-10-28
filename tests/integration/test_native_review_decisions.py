"""Real disposable PostgreSQL: independent immutable decisions, no paid dispatch."""
import asyncio
from datetime import timedelta
import json
from uuid import uuid4

import httpx
import pytest

from app.jobs_v22.errors import DeterministicJobError, TransientJobError
from app.report_v22.review_decision_service import ReviewDecisionService
from app.report_v22.review_decision_storage import ReviewDecisionRepository
from tests.integration.test_native_review_freeze import review_setup, prepared, native_database, freeze, inspect
from tests.test_v22_review_decisions import item, request
from app.jobs_v22.digest import canonical_json_bytes
from app.jobs_v22.models import utc_now
from app.report_v22.review_freeze_models import ReviewFreezeSelection
from tests.integration.test_native_review_freeze import raw_args

pytestmark=[pytest.mark.native_integration,pytest.mark.anyio,pytest.mark.timeout(70)]


@pytest.fixture
async def decisions(review_setup):
    p=review_setup; saved=await freeze(p)
    async with httpx.AsyncClient(transport=httpx.MockTransport(p["db"].transport)) as client:
        repo=ReviewDecisionRepository(url="https://local-test.invalid",service_role_key="local",http_client=client)
        p["decision_repo"]=repo; p["decision_service"]=ReviewDecisionService(repo,p["reviews"])
        p["evidence"]=saved; p["owner"]=p["identity"]|dict(report_id=saved.bundle.report.id)
        yield p


async def save(p, req):
    return await p["decision_service"].save(**p["owner"],request=req)


async def test_native_decision_roundtrip_kinds_and_evidence_unchanged(decisions):
    p=decisions; s=p["evidence"]; original=s.model_dump_json(); previous=None; requests=[]
    for kind in ("continue","adjust","observe","defer","new"):
        req=request(s,[item(s,kind,"new-work" if kind=="new" else None)],
            previous_id=str(previous.id) if previous else None,previous_checksum=previous.checksum if previous else None)
        previous=await save(p,req); requests.append(req)
        assert previous.recorded_at>=s.frozen_at
    assert previous.revision==5
    recovered=await save(p,requests[0]); assert recovered.revision==1
    assert await p["decision_service"].read(**p["owner"],review_id=s.id,decision_id=recovered.id)==recovered
    assert (await p["reviews"].read(**p["identity"],review_id=s.id)).model_dump_json()==original
    assert await inspect(p["db"],p["scenario"])==p["baseline"]


async def test_native_decision_concurrent_idempotence_and_stale_head(decisions):
    p=decisions; s=p["evidence"]; req=request(s)
    first=await asyncio.gather(*(save(p,req) for _ in range(4)))
    assert all(r==first[0] for r in first)
    old=first[0]
    choices=[request(s,[],previous_id=str(old.id),previous_checksum=old.checksum) for _ in range(2)]
    results=await asyncio.gather(*(save(p,r) for r in choices),return_exceptions=True)
    assert sum(isinstance(r,DeterministicJobError) and "STALE" in str(r) for r in results)==1
    assert len(await p["db"].query("select id from capability_review_decision_revisions"))==2
    changed=req|dict(decisions=[])
    with pytest.raises(DeterministicJobError,match="CONFLICT"): await save(p,changed)


async def test_native_decision_history_empty_clear_and_pagination(decisions):
    p=decisions; s=p["evidence"]; service=p["decision_service"]
    assert (await service.history(**p["owner"],review_id=s.id)).items==[]
    previous=None
    for _ in range(22):
        previous=await save(p,request(s,[],previous_id=str(previous.id) if previous else None,previous_checksum=previous.checksum if previous else None))
    first=await service.history(**p["owner"],review_id=s.id)
    second=await service.history(**p["owner"],review_id=s.id,cursor=first.next_cursor)
    assert len(first.items)==20 and len(second.items)==2 and second.next_cursor is None
    assert [i.revision for i in first.items+second.items]==list(range(22,0,-1))


async def test_native_decision_lost_ack_recovery(decisions):
    p=decisions; req=request(p["evidence"]); lost=False
    async def transport(r):
        nonlocal lost
        response=await p["db"].transport(r)
        if r.url.path.endswith("save_v22_review_decision") and response.status_code==200 and not lost:
            lost=True; raise httpx.ReadTimeout("lost private acknowledgement",request=r)
        return response
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        service=ReviewDecisionService(ReviewDecisionRepository(url="https://local-test.invalid",service_role_key="local",http_client=client),p["reviews"])
        with pytest.raises(TransientJobError): await service.save(**p["owner"],request=req)
        recovered=await service.save(**p["owner"],request=req)
        assert recovered.revision==1
    assert len(await p["db"].query("select id from capability_review_decision_revisions"))==1


async def test_native_decision_auth_roles_and_immutable(decisions):
    p=decisions; s=p["evidence"]; req=request(s); r=await save(p,req); service=p["decision_service"]
    for key in ("user_id","case_id","report_id"):
        wrong=p["owner"]|{key:uuid4()}
        with pytest.raises(DeterministicJobError,match="UNAVAILABLE"): await service.save(**wrong,request=req)
        with pytest.raises(DeterministicJobError,match="UNAVAILABLE"): await service.history(**wrong,review_id=s.id)
    for statement in ("update capability_review_decision_revisions set id=id where id=%s","delete from capability_review_decision_revisions where id=%s"):
        with pytest.raises(Exception,match="IMMUTABLE"): await p["db"].query(statement,[r.id])
        with pytest.raises(Exception,match="permission denied"): await p["db"].query(statement,[r.id],True)
    rights=await p["db"].query("""select has_table_privilege('service_role','capability_review_decision_revisions','INSERT') a,
      has_table_privilege('authenticated','capability_review_decision_revisions','SELECT') b,
      has_function_privilege('anon','save_v22_review_decision(uuid,uuid,uuid,uuid,text)','EXECUTE') c,
      has_function_privilege('service_role','v22_normalize_decision_request(jsonb,jsonb)','EXECUTE') d""")
    assert not any(rights[0].values())
    await p["db"].query("update client_cases set status='archived',archived_at=clock_timestamp() where id=%s",[s.case_id])
    with pytest.raises(DeterministicJobError,match="UNAVAILABLE"): await save(p,req)
    with pytest.raises(DeterministicJobError,match="UNAVAILABLE"): await service.read(**p["owner"],review_id=s.id,decision_id=r.id)
    with pytest.raises(DeterministicJobError,match="UNAVAILABLE"): await service.history(**p["owner"],review_id=s.id)


async def test_native_decision_direct_rpc_rejects_invalid(decisions):
    p=decisions; s=p["evidence"]
    def args(req): return {"p_"+k:str(v) for k,v in p["owner"].items()}|dict(p_review_id=str(s.id),p_request_text=json.dumps(req))
    for change in (dict(reason="\u2003\n"),dict(reason="x\x7f"),dict(reason="x\x00"),dict(reason="x"*1201),dict(references=["page:foreign"]),
        dict(disposition="observe"),dict(disposition="defer"),dict(source="system"),dict(target="extra"),dict(action_id="missing"),dict(other=True)):
        req=request(s); req["decisions"][0].update(change)
        with pytest.raises(Exception,match="REVIEW_DECISION_INVALID"): await p["db"].rpc("save_v22_review_decision",args(req))
    for change in (dict(decisions=[item(s)]*2),dict(recorded_at="2026-01-01T00:00:00Z"),dict(previous_id=str(uuid4())),dict(decisions=[item(s,"new",str(i)) for i in range(4)])):
        with pytest.raises(Exception,match="REVIEW_DECISION_INVALID"): await p["db"].rpc("save_v22_review_decision",args(request(s)|change))
    a=args(request(s)); a["p_request_text"]=a["p_request_text"][:-1]+',"decisions":[]}'
    with pytest.raises(Exception,match="REVIEW_DECISION_INVALID"): await p["db"].rpc("save_v22_review_decision",a)
    a["p_request_text"]=" "*65537
    with pytest.raises(Exception,match="REVIEW_DECISION_TOO_LARGE"): await p["db"].rpc("save_v22_review_decision",a)
    assert await p["db"].query("select id from capability_review_decision_revisions")==[]


async def test_native_decision_scope_conflict_and_missing_record(decisions):
    p=decisions;s=p["evidence"];req=request(s);r=await save(p,req);other=await freeze(p)
    with pytest.raises(DeterministicJobError,match="CONFLICT"): await save(p,req|dict(review_id=str(other.id)))
    with pytest.raises(DeterministicJobError,match="STALE"):
        await save(p,request(other,[],previous_id=str(r.id),previous_checksum=r.checksum))
    with pytest.raises(DeterministicJobError,match="UNAVAILABLE"):
        await p["decision_service"].read(**p["owner"],review_id=other.id,decision_id=r.id)
    with pytest.raises(DeterministicJobError,match="UNAVAILABLE"):
        await p["decision_service"].read(**p["owner"],review_id=s.id,decision_id=uuid4())
    args={"p_"+k:str(v) for k,v in p["owner"].items()}|dict(p_review_id=str(s.id),p_id=str(r.id))
    for name in ("p_user_id","p_case_id","p_report_id","p_review_id"):
        with pytest.raises(Exception,match="REVIEW_DECISION_UNAVAILABLE"):
            await p["db"].rpc("get_v22_review_decision",args|{name:str(uuid4())})


async def test_native_decision_archive_during_save_rolls_back(decisions):
    from psycopg import AsyncConnection
    p=decisions;s=p["evidence"]
    async with await AsyncConnection.connect(p["db"].url) as connection:
        await connection.execute("update client_cases set status='archived',archived_at=clock_timestamp() where id=%s",[s.case_id])
        task=asyncio.create_task(save(p,request(s)))
        await asyncio.sleep(.05)
        assert not task.done()
        await connection.commit()
        with pytest.raises(DeterministicJobError,match="UNAVAILABLE"): await asyncio.wait_for(task,5)
    assert await p["db"].query("select id from capability_review_decision_revisions")==[]


async def test_native_decision_rechecks_permissions_after_application_validation(decisions):
    p=decisions;s=p["evidence"]
    class ArchiveBeforeWrite:
        async def rpc(self,name,args):
            await p["db"].query("update client_cases set status='archived',archived_at=clock_timestamp() where id=%s",[s.case_id])
            return await p["decision_repo"].rpc(name,args)
    service=ReviewDecisionService(ArchiveBeforeWrite(),p["reviews"])
    with pytest.raises(DeterministicJobError,match="UNAVAILABLE"):
        await service.save(**p["owner"],request=request(s))
    assert await p["db"].query("select id from capability_review_decision_revisions")==[]


async def test_native_decision_future_evidence_cannot_backdate_decision(decisions):
    from uuid import UUID
    p=decisions;resolved=await p["reviews"].resolve(**p["identity"],selection=p["review_choice"])
    b=resolved.bundle.model_dump(mode="json");b["evaluated_at"]=(utc_now()+timedelta(seconds=30)).isoformat().replace("+00:00","Z")
    args=raw_args(p,resolved,body=b)
    raw=await p["db"].rpc("freeze_v22_review_evidence",args)
    future=p["reviews"].checked(canonical_json_bytes(raw),**p["identity"],review_id=UUID(args["p_id"]))
    with pytest.raises(DeterministicJobError,match="STALE"): await save(p,request(future,[]))
    assert await p["db"].query("select id from capability_review_decision_revisions")==[]


async def test_native_decision_gsc_reference_preserves_frozen_reading(decisions):
    p=decisions
    comparison=await p["repo"].freeze(**p["identity"],comparison_id=uuid4(),selection=p["selection"])
    raw=p["review_choice"].model_dump(mode="json");raw["actions"][0]["gsc_id"]=str(comparison.id)
    s=await freeze(p,selection=ReviewFreezeSelection.model_validate_json(json.dumps(raw)))
    original=s.model_dump_json();d=item(s,"adjust");d["references"]=[f"gsc:{comparison.id}"]
    await p["db"].query("update google_connections set granted_scopes='{}' where id=%s",[p["connection"]])
    count=len(p["db"].calls); result=await save(p,request(s,[d]))
    assert result.decisions[0].references==d["references"]
    assert set(p["db"].calls[count:]) <= {"get_v22_review_evidence","save_v22_review_decision"}
    assert (await p["reviews"].read(**p["identity"],review_id=s.id)).model_dump_json()==original
    assert await inspect(p["db"],p["scenario"])==p["baseline"]


async def test_native_decision_sql_python_rule_parity(native_database):
    from tests.test_v22_review_decisions import evidence_for,zero_evidence
    from tests.test_v22_review_projection_boundaries import multi_bundle
    from app.report_v22.review_decision_validation import normalize
    s=evidence_for(multi_bundle()); ds=[item(s,"defer",a.id)|dict(references=[]) for a in s.bundle.delivery.actions]
    ds += [item(s,"new",f"new-{i}") for i in range(3)]
    for saved,req in ((s,request(s,list(reversed(ds)))),(zero_evidence(),None)):
        req=req or request(saved,[])
        rows=await native_database.query("select v22_normalize_decision_request(%s::jsonb,%s::jsonb) result",
            [saved.bundle.model_dump_json(),json.dumps(req)])
        assert rows[0]["result"]==normalize(req,saved).model_dump(mode="json")
    for kind in ("continue","observe"):
        req=request(s,[item(s,kind,"ac-2")])
        with pytest.raises(Exception,match="REVIEW_DECISION_INVALID"):
            await native_database.query("select v22_normalize_decision_request(%s::jsonb,%s::jsonb)",[s.bundle.model_dump_json(),json.dumps(req)])
    d=item(s,"observe","ac-2");d["references"]=[]
    with pytest.raises(Exception,match="REVIEW_DECISION_INVALID"):
        await native_database.query("select v22_normalize_decision_request(%s::jsonb,%s::jsonb)",[s.bundle.model_dump_json(),json.dumps(request(s,[d]))])
    # Reject multi-byte payloads that fit individual field lengths but exceed bytes.
    for d in ds:
        d["reason"]="中"*1200
        if d["disposition"]=="new":d.update(target="中"*2000,work="中"*4000)
        else:d["reconsider_when"]="中"*1200
    with pytest.raises(Exception,match="REVIEW_DECISION_TOO_LARGE"):
        await native_database.query("select v22_normalize_decision_request(%s::jsonb,%s::jsonb)",[s.bundle.model_dump_json(),json.dumps(request(s,ds))])


async def test_native_zero_action_diagnostic_can_save_empty_decisions(native_database,arq_pool,redis_prefix):
    from app.jobs_v22.digest import verified_canonical_json_bytes,verified_request_digest
    from app.jobs_v22.first_service_preparation_reconciler import reconcile_v22_first_service_preparation
    from app.report_v22.review_freeze_storage import ReviewFreezeRepository
    from tests.integration.support.native_capability import seed_first_service
    from tests.integration.support.native_worker import make_context
    from tests.integration.test_native_first_service_recovery import CONFIRM,worker,wait_for,assert_completed
    db=native_database; scenario=await seed_first_service(db,arq_pool,redis_prefix,clean=True)
    await db.query(CONFIRM,scenario["confirm"],True)
    ctx=await make_context(db,arq_pool,redis_prefix,clean=True)
    try:
        await reconcile_v22_first_service_preparation(ctx)
        with worker(db,redis_prefix,clean=True) as (process,diagnostics):
            async def completed():
                state=await ctx["store"].get_state(scenario["job"])
                return state and state.status=="succeeded"
            await wait_for(completed,process,diagnostics)
        baseline=await assert_completed(db,scenario,arq_pool,redis_prefix,clean=True)
        report=baseline["reports"][0]["payload"]
        delivery=dict(schema_version="v22_delivery_revision_v1",report_id=report["id"],user_id=str(scenario["owner"]),
            case_id=str(scenario["case"]),expected_revision=0,source_checksum=verified_request_digest(report),status="approved",
            title=report["title"],summary=report["summary"],client_note=None,actions=[])
        approved=(await db.query("select * from save_v22_completed_report_revision(%s,%s,%s,%s::jsonb)",
            [scenario["owner"],scenario["case"],report["id"],json.dumps(delivery)],True))[0]
        record=dict(schema_version="v22_implementation_record_v1",id=str(uuid4()),user_id=str(scenario["owner"]),case_id=str(scenario["case"]),
            report_id=report["id"],report_checksum=delivery["source_checksum"],delivery_revision_id=str(approved["id"]),delivery_revision=1,revision=1,
            recorded_at=utc_now().isoformat().replace("+00:00","Z"),entries=[])
        canonical=verified_canonical_json_bytes(record).decode();record["checksum"]=verified_request_digest(record)
        await db.query("select save_v22_implementation_record(%s,%s,%s,%s,0,%s::jsonb,%s)",
            [scenario["owner"],scenario["case"],report["id"],approved["id"],json.dumps(record),canonical],True)
        selection=ReviewFreezeSelection.model_validate_json(json.dumps(dict(report=dict(id=report["id"],checksum=delivery["source_checksum"]),
            delivery=dict(id=str(approved["id"]),revision=1,checksum=verified_request_digest(approved["content"])),
            implementation={k:record[k] for k in ("id","revision","checksum")},actions=[])))
        async with httpx.AsyncClient(transport=httpx.MockTransport(db.transport)) as client:
            repo=ReviewFreezeRepository(url="https://local-test.invalid",service_role_key="local",http_client=client)
            identity=dict(user_id=scenario["owner"],case_id=scenario["case"])
            saved=await repo.freeze(**identity,review_id=uuid4(),selection=selection)
            service=ReviewDecisionService(ReviewDecisionRepository(url="https://local-test.invalid",service_role_key="local",http_client=client),repo)
            result=await service.save(**identity,report_id=report["id"],request=request(saved,[]))
            assert result.decisions==[] and result.revision==1
            assert await repo.read(**identity,review_id=saved.id)==saved
        assert await inspect(db,scenario)==baseline
    finally:
        await ctx["native_client"].aclose()
