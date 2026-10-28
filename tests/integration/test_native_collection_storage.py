"""Real SQL/Redis with controlled Google responses, no external accounts."""
import asyncio
from copy import deepcopy
from datetime import timedelta
import json
from uuid import uuid4

import httpx
import pytest

from app.google_connections_v22.gsc import GscProvider
from app.jobs_v22.cost_ledger import JobCostLedger
from app.jobs_v22.cost_models import PricingCatalog
from app.jobs_v22.digest import canonical_json_bytes, request_digest, verified_canonical_json_bytes, verified_request_digest
from app.jobs_v22.errors import DeterministicJobError, TransientJobError
from app.jobs_v22.models import utc_now
from app.report_v22.collection_executor import execute_collection
from app.report_v22.collection_storage import CollectionRepository, CollectionSelection
from app.report_v22.comparison_collection import collect_gsc_comparison, prepare_gsc_comparison
from app.report_v22.implementation_records import ImplementationRecord
from tests.integration.test_native_comparison_storage import prepared, native_database, inspect
from test_v22_comparison_collection import response

pytestmark = [pytest.mark.native_integration, pytest.mark.anyio, pytest.mark.timeout(50)]


@pytest.fixture
async def collection_setup(prepared, arq_pool, redis_prefix):
    p = prepared
    async with httpx.AsyncClient(transport=httpx.MockTransport(p["db"].transport)) as client:
        p["collections"] = CollectionRepository(url="https://local-test.invalid", service_role_key="local", http_client=client)
        p["choice"] = CollectionSelection(implementation=p["selection"].implementation,
            action_id=p["selection"].action_id, days=28, binding_id=p["binding"])
        p["calls"] = []
        def controlled(req):
            p["calls"].append(req)
            return response(req)
        p["provider_handler"] = controlled
        p["ledger"] = lambda job: JobCostLedger(arq_pool,prefix=redis_prefix,job_id=job.id,ttl_seconds=600,
            pricing=PricingCatalog(),job_created_at=utc_now())
        p["ordinary"] = await ordinary(p)
        yield p


async def ordinary(p):
    return [await p["db"].query(sql,[p["identity"]["case_id"]]) for sql in (
        "select * from data_snapshots where case_id=%s order by id",
        "select * from case_source_bindings where case_id=%s order by id",
        "select * from google_sync_jobs where case_id=%s order by id")]


async def request(p, *, collection_id=None, selection=None):
    return await p["collections"].request_collection(**p["identity"], collection_id=collection_id or uuid4(), selection=selection or p["choice"])


async def execute(p, job, *, handler=None, broker=None):
    class Broker:
        async def access_token(self, connection):
            assert connection == str(p["connection"])
            return "private-native-test-token"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler or p["provider_handler"])) as client:
        return await execute_collection(p["collections"],job.id,provider=GscProvider(client),
            token_broker=broker or Broker(),ledger_factory=p["ledger"])


async def material(p, claimed):
    async with httpx.AsyncClient(transport=httpx.MockTransport(p["provider_handler"])) as client:
        return await collect_gsc_comparison(claimed.record, action_id=claimed.selection.action_id, days=claimed.selection.days,
            binding_id=claimed.selection.binding_id, resource_id=claimed.plan.resource_id, at=claimed.plan.prepared_at,
            provider=GscProvider(client),token="test-token",cost_ledger=p["ledger"](claimed))


async def test_native_collection_concurrent_confirmation_claim_and_isolation(collection_setup):
    p=collection_setup; cid=uuid4()
    jobs=await asyncio.gather(*(request(p,collection_id=cid) for _ in range(6)))
    assert all(j==jobs[0] for j in jobs) and jobs[0].status=="queued"
    with pytest.raises(DeterministicJobError,match="CONFLICT"):
        await request(p,collection_id=cid,selection=p["choice"].model_copy(update={"days":7}))
    with pytest.raises(DeterministicJobError): await request(p)
    outcomes=await asyncio.gather(*(execute(p,jobs[0]) for _ in range(4)))
    successes=[x for x in outcomes if x]
    assert len(successes)==1 and successes[0].status=="succeeded" and len(p["calls"])==12
    saved=await request(p,collection_id=cid)
    assert saved==successes[0] and saved.collection.plan==jobs[0].plan
    assert "private-native-test-token" not in saved.model_dump_json()
    assert await execute(p,jobs[0]) is None and len(p["calls"])==12
    assert len(await p["db"].query("select id from capability_collections"))==1
    assert await ordinary(p)==p["ordinary"]
    assert await inspect(p["db"],p["scenario"])==p["baseline"]


async def test_native_collection_waiting_wrong_owner_scope_and_plan_tamper(collection_setup):
    p=collection_setup
    with pytest.raises(DeterministicJobError,match="WAITING"):
        await request(p,selection=p["choice"].model_copy(update={"days":90}))
    for changes in ({"binding_id":uuid4()},{"action_id":"not-an-action"},
        {"implementation":p["choice"].implementation.model_copy(update={"revision":2})}):
        with pytest.raises(DeterministicJobError): await request(p,selection=p["choice"].model_copy(update=changes))
    with pytest.raises(DeterministicJobError):
        await p["collections"].request_collection(user_id=uuid4(),case_id=p["identity"]["case_id"],collection_id=uuid4(),selection=p["choice"])
    plan=prepare_gsc_comparison(ImplementationRecord.model_validate_json(json.dumps(p["record"])),
        action_id=p["choice"].action_id,days=28,binding_id=p["binding"],resource_id="sc-domain:example.test",at=utc_now().isoformat().replace("+00:00","Z"))
    for alteration in (dict(resource_id="sc-domain:wrong.test"),dict(expected_requests=24),dict(user_id=str(uuid4())),
                       dict(windows={**plan.model_dump(mode="json")["windows"],"before":{"start":"2026-01-01","end":"2026-01-28"}})):
        body=plan.model_dump(mode="json",exclude={"checksum"})|alteration
        canonical=verified_canonical_json_bytes(body).decode();body["checksum"]=verified_request_digest(body)
        with pytest.raises(Exception,match="COLLECTION_INVALID"):
            await p["db"].rpc("request_v22_collection",dict(p_user_id=str(p["identity"]["user_id"]),p_case_id=str(p["identity"]["case_id"]),
                p_id=str(uuid4()),p_selection=p["choice"].model_dump(mode="json"),p_plan=body,p_canonical_plan=canonical))
    assert await p["db"].query("select id from capability_collection_jobs")==[] and p["calls"]==[]


async def test_native_collection_permission_changes_at_claim_and_finish(collection_setup):
    p=collection_setup;job=await request(p)
    await p["db"].query("update google_connections set status='error' where id=%s",[p["connection"]])
    assert await execute(p,job) is None and p["calls"]==[]
    saved=await request(p,collection_id=job.id)
    assert saved.status=="failed" and saved.error_code=="SYNC_BINDING_CHANGED"
    await p["db"].query("update google_connections set status='active' where id=%s",[p["connection"]])
    job=await request(p);claimed=await p["collections"].claim_collection(job.id);result=await material(p,claimed)
    await p["db"].query("update case_source_bindings set identity_match_status='needs_confirmation' where id=%s",[p["binding"]])
    saved=await p["collections"].finish_collection(claimed,result)
    assert saved.status=="failed" and saved.collection is None
    assert await p["db"].query("select id from capability_collections")==[]
    assert await inspect(p["db"],p["scenario"])==p["baseline"]


async def test_native_collection_lost_ack_recovers_without_provider_replay(collection_setup):
    p=collection_setup;repo=p["collections"];cid=uuid4();lost=set()
    async def transport(req):
        answer=await p["db"].transport(req)
        name=req.url.path.rsplit("/",1)[-1]
        if name in ("request_v22_collection","finish_v22_collection") and answer.status_code==200 and name not in lost:
            lost.add(name);raise httpx.ReadTimeout("ack lost",request=req)
        return answer
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        p["collections"]=CollectionRepository(url="https://local-test.invalid",service_role_key="local",http_client=client)
        with pytest.raises(TransientJobError): await request(p,collection_id=cid)
        job=await request(p,collection_id=cid)
        with pytest.raises(TransientJobError): await execute(p,job)
        saved=await request(p,collection_id=cid)
        assert saved.status=="succeeded" and len(p["calls"])==12
        assert await execute(p,job) is None
    p["collections"]=repo
    await p["db"].query("update google_connections set granted_scopes='{}' where id=%s",[p["connection"]])
    assert await request(p,collection_id=cid)==saved  # original history is not rewritten by revocation
    with pytest.raises(DeterministicJobError): await request(p)
    assert len(p["calls"])==12


async def test_native_collection_expiry_cancellation_and_late_worker_fencing(collection_setup):
    p=collection_setup;job=await request(p)
    started=asyncio.Event()
    async def blocked(req):
        p["calls"].append(req);started.set();await asyncio.Event().wait()
    task=asyncio.create_task(execute(p,job,handler=blocked))
    await asyncio.wait_for(started.wait(),5);task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    running=await request(p,collection_id=job.id)
    assert running.status=="running" and running.collection is None
    await p["db"].query("update capability_collection_jobs set lease_expires_at=now()-interval '1 second' where id=%s",[job.id])
    assert await execute(p,job) is None
    failed=await request(p,collection_id=job.id)
    assert failed.status=="failed" and failed.error_code=="SYNC_INTERRUPTED" and len(p["calls"])==1
    # Even a correct previous lease cannot publish after recovery made failure terminal.
    with pytest.raises(Exception,match="COLLECTION_LEASE_LOST"):
        await p["db"].rpc("finish_v22_collection",dict(p_id=str(job.id),p_lease_id=str(running.lease_id),p_payload={},p_canonical_payload="{}",p_canonical_snapshot="{}"))
    await p["collections"].fail_collection(running,"SYNC_FAILED")
    assert await request(p,collection_id=job.id)==failed
    assert await ordinary(p)==p["ordinary"]


async def test_native_collection_failure_no_partial_publish_and_safe_error(collection_setup):
    p=collection_setup;job=await request(p)
    def unavailable(req):
        p["calls"].append(req)
        return httpx.Response(503,json={"secret":"private-provider-detail"}) if len(p["calls"])==3 else response(req)
    failed=await execute(p,job,handler=unavailable)
    assert failed.status=="failed" and failed.error_code=="SYNC_GOOGLE_UNAVAILABLE"
    assert failed.collection is None and "private-provider-detail" not in failed.model_dump_json()
    assert await execute(p,job) is None and len(p["calls"])==3
    next_job=await request(p)
    claimed=await p["collections"].claim_collection(next_job.id)
    await p["collections"].fail_collection(claimed,"SYNC_SECRET_ACCESS_TOKEN_ABC")
    saved=await request(p,collection_id=next_job.id)
    assert saved.error_code=="SYNC_FAILED"
    assert await p["db"].query("select id from capability_collections")==[]
    assert await inspect(p["db"],p["scenario"])==p["baseline"]


async def test_native_collection_result_integrity_privileges_and_version_pinning(collection_setup):
    p=collection_setup;job=await request(p);claimed=await p["collections"].claim_collection(job.id)
    result=await material(p,claimed)
    forged=result.model_dump(mode="json",exclude={"checksum"})
    forged["snapshot"]["resource_id"]="sc-domain:wrong.test"
    forged["payload_checksum"]=request_digest(forged["snapshot"])
    canonical=verified_canonical_json_bytes(forged).decode();forged["checksum"]=verified_request_digest(forged)
    with pytest.raises(Exception,match="COLLECTION_INVALID"):
        await p["db"].rpc("finish_v22_collection",dict(p_id=str(job.id),p_lease_id=str(claimed.lease_id),p_payload=forged,
            p_canonical_payload=canonical,p_canonical_snapshot=canonical_json_bytes(forged["snapshot"]).decode()))
    saved=await p["collections"].finish_collection(claimed,result)
    assert await p["collections"].finish_collection(claimed,result)==saved
    with pytest.raises(DeterministicJobError):
        await p["collections"].finish_collection(claimed.model_copy(update={"lease_id":uuid4()}),result)
    with pytest.raises(Exception,match="IMMUTABLE"):
        await p["db"].query("update capability_collections set payload='{}' where id=%s",[job.id])
    rights=await p["db"].query("""select
      has_table_privilege('service_role','capability_collection_jobs','UPDATE') job_write,
      has_table_privilege('service_role','capability_collections','INSERT') material_write,
      has_function_privilege('authenticated','request_v22_collection(uuid,uuid,uuid,jsonb,jsonb,text)','EXECUTE') request,
      has_function_privilege('anon','get_v22_collection(uuid,uuid,uuid)','EXECUTE') read,
      has_function_privilege('service_role','v22_lock_collection(uuid)','EXECUTE') helper""")
    assert not any(rights[0].values())
    correction=deepcopy(p["record"]);correction.pop("checksum")
    correction.update(revision=2,recorded_at=utc_now().isoformat().replace("+00:00","Z"))
    correction["entries"][0]["implemented_on"]=(utc_now()-timedelta(days=45)).date().isoformat()
    canonical=verified_canonical_json_bytes(correction).decode();correction["checksum"]=verified_request_digest(correction)
    await p["db"].query("select save_v22_implementation_record(%s,%s,%s,%s,1,%s::jsonb,%s)",
        [p["identity"]["user_id"],p["identity"]["case_id"],correction["report_id"],correction["delivery_revision_id"],json.dumps(correction),canonical],True)
    assert await request(p,collection_id=job.id)==saved
    with pytest.raises(DeterministicJobError): await request(p)


@pytest.mark.parametrize("zone",["Asia/Shanghai","America/Los_Angeles","Pacific/Kiritimati"])
async def test_native_collection_sql_python_timezone_agreement(collection_setup,zone):
    p=collection_setup;record=deepcopy(p["record"]);record.pop("checksum")
    record.update(revision=2,recorded_at=utc_now().isoformat().replace("+00:00","Z"))
    record["entries"][0]["timezone"]=zone
    canonical=verified_canonical_json_bytes(record).decode();record["checksum"]=verified_request_digest(record)
    await p["db"].query("select save_v22_implementation_record(%s,%s,%s,%s,1,%s::jsonb,%s)",
        [p["identity"]["user_id"],p["identity"]["case_id"],record["report_id"],record["delivery_revision_id"],json.dumps(record),canonical],True)
    selection=CollectionSelection.model_validate_json(json.dumps({**p["choice"].model_dump(mode="json"),
        "implementation":{k:record[k] for k in ("id","revision","checksum")}}))
    job=await request(p,selection=selection)
    assert job.plan.windows.timezone=="America/Los_Angeles" and job.status=="queued"
    assert (job.plan.windows.excluded.end-job.plan.windows.excluded.start).days==(0 if zone=="America/Los_Angeles" else 1)


@pytest.mark.parametrize("change",["case_version","resource","scope"])
async def test_native_collection_rechecks_context_before_material_commit(collection_setup,change):
    p=collection_setup;job=await request(p);claimed=await p["collections"].claim_collection(job.id)
    result=await material(p,claimed)
    if change=="case_version":
        await p["db"].query("update client_cases set updated_at=clock_timestamp() where id=%s",[p["identity"]["case_id"]])
    elif change=="resource":
        await p["db"].query("update case_source_bindings set external_resource_id='sc-domain:changed.test' where id=%s",[p["binding"]])
    else:
        await p["db"].query("update google_connections set granted_scopes='{}' where id=%s",[p["connection"]])
    failed=await p["collections"].finish_collection(claimed,result)
    assert failed.status=="failed" and failed.error_code=="SYNC_BINDING_CHANGED"
    assert await p["db"].query("select id from capability_collections")==[]
    assert await inspect(p["db"],p["scenario"])==p["baseline"]


async def test_native_collection_lost_claim_ack_never_reissues_external_calls(collection_setup):
    p=collection_setup;job=await request(p);lost=False
    async def transport(req):
        nonlocal lost
        answer=await p["db"].transport(req)
        if req.url.path.endswith("/claim_v22_collection") and answer.status_code==200 and not lost:
            lost=True;raise httpx.ReadTimeout("lost claim acknowledgement",request=req)
        return answer
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        p["collections"]=CollectionRepository(url="https://local-test.invalid",service_role_key="local",http_client=client)
        with pytest.raises(TransientJobError): await execute(p,job)
        assert await execute(p,job) is None and p["calls"]==[]
        await p["db"].query("update capability_collection_jobs set lease_expires_at=now()-interval '1 second' where id=%s",[job.id])
        assert await execute(p,job) is None and p["calls"]==[]
        failed=await request(p,collection_id=job.id)
        assert failed.status=="failed" and failed.error_code=="SYNC_INTERRUPTED"
