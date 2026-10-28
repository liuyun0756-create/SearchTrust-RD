"""Real SQL locks + Redis + ARQ processes; providers remain controlled fixtures."""
import asyncio
from contextlib import contextmanager
from datetime import timedelta
import os
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from time import monotonic
from urllib.parse import urlunsplit
from uuid import uuid4

import pytest

from app.jobs_v22.first_service_preparation_reconciler import reconcile_v22_first_service_preparation
from app.jobs_v22.reconciler import reconcile_once
from app.jobs_v22.worker import execute_v22_job
from app.jobs_v22.models import utc_now
from tests.integration.support.native_capability import NativeDatabase, validate_database_url, seed_first_service
from tests.integration.support.native_worker import make_context, queue_name, signal_key

pytestmark=[pytest.mark.native_integration,pytest.mark.anyio,pytest.mark.timeout(40)]
ROOT=Path(__file__).resolve().parents[2]
CONFIRM="select confirm_v22_first_service_entry(%s,%s,%s,%s,%s,%s,%s)"
CONFIRM_PLANNING="select confirm_v22_first_service_entry_with_planning(%s,%s,%s,%s,%s,%s,%s,%s)"
SAVE_PLANNING="select save_v22_case_planning_settings(%s,%s,%s,%s::jsonb) value"


def planning(priority="contact_path"):
    return dict(schema_version="v22_planning_settings_v1",priority=priority,target_pages=[],
        execution_conditions=dict(capacity="limited",publishing_access="needs_approval"))


@pytest.fixture
async def native_database():
    if not os.environ.get("V22_TEST_POSTGRES_URL"):
        pytest.skip("Use run_v22_native_integration.py for disposable native services")
    psycopg=pytest.importorskip("psycopg")
    from psycopg import sql
    original=validate_database_url(os.environ["V22_TEST_POSTGRES_URL"],os.environ)
    name="searchtrust_v22_test_"+uuid4().hex
    admin=urlunsplit(original._replace(path="/postgres"))
    with psycopg.connect(admin,autocommit=True) as connection:
        connection.execute(sql.SQL("create database {} template {}").format(sql.Identifier(name),sql.Identifier(original.path[1:])))
    try:
        yield NativeDatabase(urlunsplit(original._replace(path="/"+name)))
    finally:
        with psycopg.connect(admin,autocommit=True) as connection:
            connection.execute(sql.SQL("drop database {} with (force)").format(sql.Identifier(name)))


@contextmanager
def worker(database,prefix,*,clean=False,crash=""):
    environment={**os.environ,"V22_NATIVE_DATABASE_URL":database.url,"V22_NATIVE_PREFIX":prefix,
        "V22_NATIVE_CLEAN":"1" if clean else "0","V22_NATIVE_CRASH":crash,
        "PYTHONPATH":os.pathsep.join((str(ROOT),str(ROOT/"tests"),os.environ.get("PYTHONPATH","")))}
    with tempfile.TemporaryFile() as output:
        process=subprocess.Popen([sys.executable,"-m","arq","tests.integration.support.native_worker.NativeWorkerSettings"],
            cwd=ROOT,env=environment,stdout=output,stderr=subprocess.STDOUT)
        def diagnostics():
            output.flush();output.seek(0)
            return output.read().decode(errors="replace")[-5000:]
        try:
            yield process,diagnostics
        finally:
            if process.poll() is None:
                process.terminate()
                try: process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill();process.wait(timeout=5)


async def wait_for(check,process,diagnostics,*,timeout=15):
    deadline=monotonic()+timeout
    while monotonic()<deadline:
        result=await check()
        if result:return result
        if process.poll() is not None:
            # os._exit occurs just after publishing the durable signal.
            result=await check()
            if result:return result
            raise AssertionError(f"Native worker exited ({process.returncode}):\n{diagnostics()}")
        await asyncio.sleep(.05)
    raise AssertionError(f"Native worker did not finish:\n{diagnostics()}")


async def inspect(database,scenario):
    owner,job=scenario["owner"],scenario["job"]
    return dict(balance=(await database.query("select credit_balance from users where id=%s",[owner]))[0]["credit_balance"],
        ledger=await database.query("select kind,delta from credit_ledger where workflow_charge_id=%s order by created_at,kind",[job]),
        charge=(await database.query("select state,final_capability_report_id from workflow_charges where id=%s",[job]))[0],
        reports=await database.query("select payload from capability_report_versions where case_id=%s",[scenario["case"]]),
        jobs=await database.query("select id,status,run_generation,created_at,deadline_at,attempt_count from analysis_jobs where case_id=%s",[scenario["case"]]))


async def assert_completed(database,scenario,redis,prefix,*,clean=False):
    state=await inspect(database,scenario)
    assert state["balance"]==4 and state["ledger"]==[{"kind":"verified_debit","delta":-1}]
    assert state["charge"]==dict(state="consumed",final_capability_report_id=scenario["job"])
    assert len(state["jobs"])==len(state["reports"])==1
    report=state["reports"][0]["payload"]
    assert report["input_manifest"]["mode"]=="first_service" and report["input_manifest"]["previous_report"] is None
    assert len(report["input_manifest"]["snapshots"])==6
    assert len(report["actions"])==(0 if clean else 3)
    assert [await redis.get(f"{prefix}:provider:{kind}") for kind in ("site","competitor","public_gbp")]==[b"1"]*3
    assert await redis.get(f"{prefix}:rpc:complete_v22_capability_analysis")==b"1"
    assert await redis.get(f"{prefix}:rpc:freeze_v22_first_service_preparation")==b"1"
    return state


async def test_concurrent_confirmations_create_one_scope_job_and_debit(native_database,arq_pool,redis_prefix):
    scenario=await seed_first_service(native_database,arq_pool,redis_prefix)
    await asyncio.gather(*(native_database.query(CONFIRM,scenario["confirm"],True) for _ in range(20)))
    state=await inspect(native_database,scenario)
    assert state["balance"]==4 and state["ledger"]==[{"kind":"verified_debit","delta":-1}]
    assert len(state["jobs"])==1
    scopes=await native_database.query("select scope from case_business_scope_versions where case_id=%s",[scenario["case"]])
    assert len(scopes)==1 and scopes[0]["scope"]["confirmed_at"].endswith("Z")


async def test_concurrent_planning_saves_never_overwrite_a_different_version(native_database,arq_pool,redis_prefix):
    scenario=await seed_first_service(native_database,arq_pool,redis_prefix)
    results=await asyncio.gather(*(native_database.query(SAVE_PLANNING,[scenario["owner"],scenario["case"],0,json.dumps(planning(p))],True)
        for p in ("contact_path","business_identity")),return_exceptions=True)
    successes=[r for r in results if not isinstance(r,Exception)]
    failures=[r for r in results if isinstance(r,Exception)]
    assert len(successes)==len(failures)==1 and failures[0].sqlstate=="40001"
    assert len(await native_database.query("select revision from case_planning_settings where case_id=%s",[scenario["case"]]))==1
    state=await inspect(native_database,scenario)
    assert state["balance"]==4 and state["ledger"]==[{"kind":"verified_debit","delta":-1}] and not state["jobs"]


async def test_planning_save_races_confirmation_without_deadlock_or_silent_replacement(native_database,arq_pool,redis_prefix):
    scenario=await seed_first_service(native_database,arq_pool,redis_prefix)
    await native_database.query(SAVE_PLANNING,[scenario["owner"],scenario["case"],0,json.dumps(planning())],True)
    async def confirm():return await native_database.query(CONFIRM_PLANNING,[*scenario["confirm"],1],True)
    async def update():return await native_database.query(SAVE_PLANNING,[scenario["owner"],scenario["case"],1,json.dumps(planning("business_identity"))],True)
    results=await asyncio.gather(*(confirm() for _ in range(8)),*(update() for _ in range(8)),return_exceptions=True)
    assert all(not isinstance(r,Exception) or r.sqlstate=="40001" for r in results)
    before=await native_database.query("select scope from case_business_scope_versions where case_id=%s",[scenario["case"]])
    await native_database.query(CONFIRM_PLANNING,[*scenario["confirm"],2],True)
    after=await native_database.query("select scope from case_business_scope_versions where case_id=%s",[scenario["case"]])
    assert len(after)==1
    if before: assert before==after and after[0]["scope"]["priority"]=="contact_path"
    else: assert after[0]["scope"]["priority"]=="business_identity"
    state=await inspect(native_database,scenario)
    assert state["balance"]==4 and len(state["jobs"])==1 and state["ledger"]==[{"kind":"verified_debit","delta":-1}]


async def test_concurrent_preparation_leases_have_one_owner_and_fence_the_old_owner(native_database,arq_pool,redis_prefix):
    scenario=await seed_first_service(native_database,arq_pool,redis_prefix)
    await native_database.query(CONFIRM,scenario["confirm"],True)
    tokens=[uuid4() for _ in range(12)]
    async def claim(token):return await native_database.rpc("claim_v22_first_service_preparation",dict(
        p_job_id=str(scenario["job"]),p_run_generation=1,p_lease_token=str(token),p_max_attempts=3))
    results=await asyncio.gather(*(claim(token) for token in tokens))
    assert [r["outcome"] for r in results].count("claimed")==1
    assert [r["outcome"] for r in results].count("busy")==11
    winner=tokens[next(i for i,r in enumerate(results) if r["outcome"]=="claimed")]
    await native_database.query("update capability_first_service_runs set lease_until=clock_timestamp()+interval '100 milliseconds' where job_id=%s",[scenario["job"]])
    await asyncio.sleep(.15)
    next_claim=await claim(uuid4());assert next_claim["outcome"]=="claimed" and next_claim["attempt_count"]==2
    assert await native_database.rpc("heartbeat_v22_first_service_preparation",dict(p_job_id=str(scenario["job"]),p_run_generation=1,
        p_lease_token=str(winner),p_cost_counters={"cost_ledger_revision":1})) is False
    assert (await inspect(native_database,scenario))["balance"]==4


@pytest.mark.parametrize("clean",[False,True])
async def test_real_arq_preparation_and_generation_deliver_one_report(native_database,arq_pool,redis_prefix,clean):
    scenario=await seed_first_service(native_database,arq_pool,redis_prefix,clean=clean)
    await native_database.query(SAVE_PLANNING,[scenario["owner"],scenario["case"],0,json.dumps(planning())],True)
    await native_database.query(CONFIRM_PLANNING,[*scenario["confirm"],1],True)
    await native_database.query(SAVE_PLANNING,[scenario["owner"],scenario["case"],1,json.dumps(planning("business_identity"))],True)
    ctx=await make_context(native_database,arq_pool,redis_prefix,clean=clean)
    try:
        await reconcile_v22_first_service_preparation(ctx);await reconcile_v22_first_service_preparation(ctx)
        assert await arq_pool.zcard(queue_name(redis_prefix))==1
        with worker(native_database,redis_prefix,clean=clean) as (process,diagnostics):
            async def completed():
                state=await ctx["store"].get_state(scenario["job"])
                return state and state.status=="succeeded"
            await wait_for(completed,process,diagnostics)
        state=await assert_completed(native_database,scenario,arq_pool,redis_prefix,clean=clean)
        scope=state["reports"][0]["payload"]["input_manifest"]["business"]
        assert scope["priority"]=="contact_path" and scope["execution_conditions"]==planning()["execution_conditions"]
    finally:await ctx["native_client"].aclose()


@pytest.mark.parametrize("phase",["freeze","checkpoint","commit"])
async def test_process_death_recovers_the_same_task_without_recollection_or_refund(native_database,arq_pool,redis_prefix,phase):
    scenario=await seed_first_service(native_database,arq_pool,redis_prefix)
    await native_database.query(CONFIRM,scenario["confirm"],True)
    before=(await inspect(native_database,scenario))["jobs"][0]
    ctx=await make_context(native_database,arq_pool,redis_prefix)
    try:
        publication_time=utc_now()
        ctx["clock"]=lambda:publication_time
        await reconcile_v22_first_service_preparation(ctx)
        with worker(native_database,redis_prefix,crash=phase) as (process,diagnostics):
            await wait_for(lambda:arq_pool.get(signal_key(redis_prefix,phase)),process,diagnostics)
            await asyncio.to_thread(process.wait,5)
            assert process.returncode==71
        manifests=await native_database.query("select input_checksum from analysis_input_manifests where job_id=%s",[scenario["job"]])
        assert len(manifests)==1
        checkpoints={key:await arq_pool.get(key) async for key in arq_pool.scan_iter(
            match=f"{redis_prefix}:job:{scenario['job']}:checkpoint:*")}
        if phase in {"checkpoint","commit"}:assert len(checkpoints)==1
        if phase=="freeze":
            assert await ctx["store"].get_state(scenario["job"]) is None
            await reconcile_v22_first_service_preparation(ctx)
            # Same-minute recovery is deliberately deduplicated by real ARQ.
            # Advance only the publication bucket, keeping SQL/task time intact.
            ctx["clock"]=lambda:publication_time+timedelta(seconds=61)
            await reconcile_v22_first_service_preparation(ctx)
        else:
            interrupted=await ctx["store"].require_state(scenario["job"])
            assert interrupted.status=="running"
            await reconcile_once(store=ctx["store"],queue=ctx["native_queue"],synchronizer=ctx["callback_synchronizer"],
                now=interrupted.heartbeat_at+timedelta(seconds=181),stale_seconds=180,max_attempts=3,
                capability_terminal=ctx["capability_terminal_coordinator"])
        if phase!="commit":
            with worker(native_database,redis_prefix) as (process,diagnostics):
                async def completed():
                    state=await ctx["store"].get_state(scenario["job"])
                    return state and state.status=="succeeded"
                await wait_for(completed,process,diagnostics)
        result=await assert_completed(native_database,scenario,arq_pool,redis_prefix)
        assert result["jobs"][0]["created_at"]==before["created_at"] and result["jobs"][0]["deadline_at"]==before["deadline_at"]
        assert result["jobs"][0]["run_generation"]==(2 if phase=="checkpoint" else 1)
        assert await native_database.query("select input_checksum from analysis_input_manifests where job_id=%s",[scenario["job"]])==manifests
        for key,payload in checkpoints.items():
            assert await arq_pool.get(key)==payload
            assert result["reports"][0]["payload"]==json.loads(payload)["result"]
        completed=await ctx["store"].require_state(scenario["job"])
        assert completed.status=="succeeded"
        await execute_v22_job(ctx,str(scenario["job"]),completed.run_generation)
        assert await arq_pool.get(f"{redis_prefix}:rpc:complete_v22_capability_analysis")==b"1"
    finally:await ctx["native_client"].aclose()


async def test_real_worker_technical_failure_refunds_once_and_stays_terminal(native_database,arq_pool,redis_prefix):
    scenario=await seed_first_service(native_database,arq_pool,redis_prefix)
    await native_database.query(CONFIRM,scenario["confirm"],True)
    ctx=await make_context(native_database,arq_pool,redis_prefix)
    try:
        await reconcile_v22_first_service_preparation(ctx)
        with worker(native_database,redis_prefix,crash="failure") as (process,diagnostics):
            async def failed():
                state=await ctx["store"].get_state(scenario["job"])
                return state and state.status=="failed"
            await wait_for(failed,process,diagnostics)
        before=await inspect(native_database,scenario)
        assert before["balance"]==5 and before["charge"]==dict(state="compensated",final_capability_report_id=None)
        assert sorted(before["ledger"],key=lambda r:r["delta"])==[
            {"kind":"verified_debit","delta":-1},{"kind":"technical_failure_credit","delta":1}]
        assert before["reports"]==[] and len(before["jobs"])==1
        await execute_v22_job(ctx,str(scenario["job"]),1)
        await reconcile_v22_first_service_preparation(ctx)
        assert await inspect(native_database,scenario)==before
    finally:await ctx["native_client"].aclose()


@pytest.mark.parametrize("competing", [False, True])
async def test_native_implementation_saves_are_free_pinned_and_concurrency_safe(native_database, arq_pool, redis_prefix, competing):
    from app.jobs_v22.digest import verified_canonical_json_bytes, verified_request_digest
    from app.report_v22.capability import CapabilityReport, DeliveryRevision
    from app.report_v22.implementation_records import parse_implementation_record

    scenario = await seed_first_service(native_database, arq_pool, redis_prefix)
    await native_database.query(CONFIRM, scenario["confirm"], True)
    ctx = await make_context(native_database, arq_pool, redis_prefix)
    try:
        await reconcile_v22_first_service_preparation(ctx)
        with worker(native_database, redis_prefix) as (process, diagnostics):
            async def completed():
                state = await ctx["store"].get_state(scenario["job"])
                return state and state.status == "succeeded"
            await wait_for(completed, process, diagnostics)
        before = await assert_completed(native_database, scenario, arq_pool, redis_prefix)
        raw_report = before["reports"][0]["payload"]
        report = CapabilityReport.model_validate_json(json.dumps(raw_report))
        delivery = dict(schema_version="v22_delivery_revision_v1", report_id=str(report.id),
            user_id=str(scenario["owner"]), case_id=str(scenario["case"]), expected_revision=0,
            source_checksum=verified_request_digest(raw_report), status="approved", title=report.title,
            summary=report.summary, client_note=None,
            actions=[{k:v for k,v in action.items() if k not in {"finding_ids", "dependencies"}} for action in raw_report["actions"]])
        saved_delivery = (await native_database.query("select * from save_v22_completed_report_revision(%s,%s,%s,%s::jsonb)",
            [scenario["owner"], scenario["case"], report.id, json.dumps(delivery)], True))[0]
        now = utc_now()
        body = dict(schema_version="v22_implementation_record_v1", id=str(uuid4()), user_id=str(scenario["owner"]),
            case_id=str(scenario["case"]), report_id=str(report.id), report_checksum=delivery["source_checksum"],
            delivery_revision_id=str(saved_delivery["id"]), delivery_revision=1, revision=1,
            recorded_at=now.isoformat().replace("+00:00", "Z"), entries=[dict(action_id=delivery["actions"][0]["id"],
                scope=delivery["actions"][0]["target"], implemented_on=(now-timedelta(days=1)).date().isoformat(),
                timezone="UTC", note=None, responsible=None)])
        async def save(index):
            content = json.loads(json.dumps(body))
            if competing: content["entries"][0]["note"] = f"Different tab {index}"
            payload = dict(content, checksum=verified_request_digest(content))
            return (await native_database.query("select save_v22_implementation_record(%s,%s,%s,%s,0,%s::jsonb,%s) value",
                [scenario["owner"], scenario["case"], report.id, saved_delivery["id"], json.dumps(payload), verified_canonical_json_bytes(content).decode()], True))[0]["value"]
        results = await asyncio.gather(*(save(i) for i in range(8)), return_exceptions=True)
        successful = [result for result in results if isinstance(result, dict)]
        errors = [result for result in results if isinstance(result, Exception)]
        assert len(successful) == (1 if competing else 8)
        assert all(getattr(error, "sqlstate", None) == "40001" for error in errors)
        rows = await native_database.query("select * from get_v22_implementation_records(%s,%s,%s,%s)",
            [scenario["owner"], scenario["case"], report.id, saved_delivery["id"]], True)
        assert len(rows) == 1 and rows[0]["payload"] == successful[0]
        parsed = parse_implementation_record(rows[0]["payload"], report, user_id=scenario["owner"], case_id=scenario["case"],
            delivery_revision_id=saved_delivery["id"], delivery_revision=1,
            delivery_content=DeliveryRevision.model_validate_json(json.dumps(delivery)))
        assert parsed.revision == 1
        assert await inspect(native_database, scenario) == before
    finally:
        await ctx["native_client"].aclose()
