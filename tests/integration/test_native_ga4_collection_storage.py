"""Real owner SQL/Redis, controlled Google transport, no external accounts."""
import asyncio
from copy import deepcopy
from datetime import timedelta
import json
from uuid import uuid4

import httpx
import pytest

from app.google_connections_v22.ga4_comparison import Ga4ComparisonProvider
from app.jobs_v22.cost_ledger import JobCostLedger
from app.jobs_v22.cost_models import PricingCatalog
from app.jobs_v22.digest import verified_canonical_json_bytes, verified_request_digest
from app.jobs_v22.errors import DeterministicJobError, TransientJobError
from app.jobs_v22.models import utc_now
from app.report_v22.ga4_collection_executor import execute_ga4_collection
from app.report_v22.ga4_collection_storage import Ga4CollectionRepository, Ga4CollectionSelection
from app.report_v22.ga4_comparison_collection import collect_ga4_comparison
from app.report_v22.first_party_findings_models import TrustedFirstPartySnapshot
from tests.integration.test_native_comparison_storage import prepared, native_database, inspect, insert_snapshot
from tests.integration.test_native_collection_storage import ordinary
from tests.support.comparison_fixtures import ga4_request_raw, resign_source
from tests.support.ga4_comparison_fixtures import google

pytestmark = [pytest.mark.native_integration, pytest.mark.anyio, pytest.mark.timeout(50)]


@pytest.fixture
async def ga4_setup(prepared, arq_pool, redis_prefix):
    p = prepared
    binding = (await p["db"].query("select * from case_source_bindings where case_id=%s and source_type='ga4' and is_active",
        [p["identity"]["case_id"]]))[0]
    domain = (await p["db"].query("select normalized_domain from client_cases where id=%s", [p["identity"]["case_id"]]))[0]["normalized_domain"]
    raw = ga4_request_raw()["after"]
    raw.update(snapshot_id=str(uuid4()), case_id=str(p["identity"]["case_id"]), binding_id=str(binding["id"]),
        external_resource_id=binding["external_resource_id"], fetched_at=utc_now().isoformat(), expires_at=(utc_now()+timedelta(days=7)).isoformat())
    raw["normalized_payload"].update(resource_id=binding["external_resource_id"], host_filter=[domain])
    resign_source(raw)
    source = TrustedFirstPartySnapshot.model_validate_json(json.dumps(raw))
    await insert_snapshot(p["db"], source)
    await p["db"].query("update case_source_bindings set health_status=%s,health_reasons=%s::jsonb where id=%s",
        [source.health_status, json.dumps(source.health_reasons), binding["id"]])
    p.update(binding=binding["id"], connection=binding["connection_id"], source=source, calls=[], domain=domain)
    p["choice"] = Ga4CollectionSelection(implementation=p["selection"].implementation, action_id=p["selection"].action_id,
        days=28, binding_id=p["binding"], scope_snapshot_id=source.snapshot_id, landing_path=None)
    p["ledger"] = lambda job: JobCostLedger(arq_pool, prefix=redis_prefix, job_id=job.id, ttl_seconds=600,
        pricing=PricingCatalog(), job_created_at=utc_now())
    def controlled(req):
        p["calls"].append(req)
        answer = google(req).json()
        # The test account property may differ from the synthetic contract's property.
        if req.method == "GET": answer["keyEvents"][0]["name"] = binding["external_resource_id"]+"/keyEvents/1"
        return httpx.Response(200, json=answer)
    p["provider_handler"] = controlled
    p["ordinary"] = await ordinary(p)
    async with httpx.AsyncClient(transport=httpx.MockTransport(p["db"].transport)) as client:
        p["collections"] = Ga4CollectionRepository(url="https://local-test.invalid", service_role_key="local", http_client=client)
        yield p


async def request(p, *, collection_id=None, selection=None):
    return await p["collections"].request_collection(**p["identity"], collection_id=collection_id or uuid4(), selection=selection or p["choice"])


async def execute(p, job, *, handler=None):
    class Broker:
        async def access_token(self, connection):
            assert connection == str(p["connection"])
            return "private-native-token"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler or p["provider_handler"])) as client:
        return await execute_ga4_collection(p["collections"], job.id, provider=Ga4ComparisonProvider(client),
            token_broker=Broker(), ledger_factory=p["ledger"])


async def material(p, job):
    async with httpx.AsyncClient(transport=httpx.MockTransport(p["provider_handler"])) as client:
        return await collect_ga4_comparison(job.record, action_id=job.selection.action_id, days=job.selection.days,
            binding_id=job.selection.binding_id, scope=job.plan.scope, at=job.plan.prepared_at,
            provider=Ga4ComparisonProvider(client), token="test-token", cost_ledger=p["ledger"](job))


async def revise(p, *, zone="Asia/Shanghai"):
    record = deepcopy(p["record"]); record.pop("checksum")
    record.update(revision=2, recorded_at=utc_now().isoformat().replace("+00:00", "Z"))
    record["entries"][0]["timezone"] = zone
    canonical = verified_canonical_json_bytes(record).decode(); record["checksum"] = verified_request_digest(record)
    await p["db"].query("select save_v22_implementation_record(%s,%s,%s,%s,1,%s::jsonb,%s)",
        [p["identity"]["user_id"],p["identity"]["case_id"],record["report_id"],record["delivery_revision_id"],json.dumps(record),canonical], True)
    return Ga4CollectionSelection.model_validate_json(json.dumps({**p["choice"].model_dump(mode="json"),
        "implementation":{k:record[k] for k in ("id","revision","checksum")}}))


async def test_native_ga4_concurrent_confirmation_single_claim_and_isolation(ga4_setup):
    p=ga4_setup; cid=uuid4()
    jobs=await asyncio.gather(*(request(p,collection_id=cid) for _ in range(6)))
    assert all(j==jobs[0] for j in jobs) and jobs[0].status=="queued"
    assert jobs[0].plan.scope.hosts==[p["domain"]]
    with pytest.raises(DeterministicJobError,match="CONFLICT"):
        await request(p,collection_id=cid,selection=p["choice"].model_copy(update={"landing_path":"/service"}))
    with pytest.raises(DeterministicJobError): await request(p)
    pending=await p["db"].rpc("pending_v22_ga4_collections",{})
    assert pending==[str(cid)]
    outcomes=await asyncio.gather(*(execute(p,jobs[0]) for _ in range(4)))
    successes=[x for x in outcomes if x]
    assert len(successes)==1 and successes[0].status=="succeeded" and len(p["calls"])==8
    saved=await request(p,collection_id=cid)
    assert saved==successes[0] and saved.collection.qualification.effect_conclusion is None
    assert all(m.state=="not_comparable" for m in saved.collection.qualification.metrics)
    assert "private-native-token" not in saved.model_dump_json()
    assert await execute(p,jobs[0]) is None and len(p["calls"])==8
    assert await p["db"].rpc("pending_v22_ga4_collections",{})==[]
    assert await p["db"].query("select id from capability_collections")==[]
    assert await ordinary(p)==p["ordinary"]
    assert await inspect(p["db"],p["scenario"])==p["baseline"]


async def test_native_ga4_waiting_owner_selection_and_resigned_scope_tamper(ga4_setup):
    p=ga4_setup
    with pytest.raises(DeterministicJobError,match="WAITING"):
        await request(p,selection=p["choice"].model_copy(update={"days":90}))
    for changes in ({"binding_id":uuid4()},{"scope_snapshot_id":uuid4()},{"scope_snapshot_id":p["sources"][1].snapshot_id},
        {"action_id":"not-an-action"},{"implementation":p["choice"].implementation.model_copy(update={"revision":2})}):
        with pytest.raises(DeterministicJobError): await request(p,selection=p["choice"].model_copy(update=changes))
    with pytest.raises(DeterministicJobError):
        await p["collections"].request_collection(user_id=uuid4(),case_id=p["identity"]["case_id"],collection_id=uuid4(),selection=p["choice"])
    job=await request(p)
    for scope in (dict(resource_id="properties/999"),dict(hosts=["other.test"]),dict(timezone="UTC"),dict(channel="all")):
        body=job.plan.model_dump(mode="json",exclude={"checksum"});body["scope"].update(scope)
        canonical=verified_canonical_json_bytes(body).decode();body["checksum"]=verified_request_digest(body)
        with pytest.raises(Exception,match="COLLECTION_INVALID"):
            await p["db"].rpc("request_v22_ga4_collection",dict(p_user_id=str(p["identity"]["user_id"]),p_case_id=str(p["identity"]["case_id"]),
                p_id=str(uuid4()),p_selection=p["choice"].model_dump(mode="json"),p_plan=body,p_canonical_plan=canonical))
    assert p["calls"]==[]


@pytest.mark.parametrize("zone",["Asia/Shanghai","America/Los_Angeles","Pacific/Kiritimati"])
@pytest.mark.parametrize("path",[None,"/services/repair"])
async def test_native_ga4_sql_python_windows_and_exact_path(ga4_setup,zone,path):
    p=ga4_setup;selection=await revise(p,zone=zone)
    job=await request(p,selection=selection.model_copy(update={"landing_path":path}))
    assert job.plan.windows.timezone=="America/Los_Angeles"
    assert (job.plan.windows.excluded.end-job.plan.windows.excluded.start).days==(0 if zone=="America/Los_Angeles" else 1)
    saved=await execute(p,job)
    assert saved.status=="succeeded"
    for req in p["calls"]:
        if req.method=="POST":
            filters=json.loads(req.content)["dimensionFilter"]["andGroup"]["expressions"]
            assert filters[1]["filter"]["inListFilter"]["values"]==[p["domain"]]
            if path: assert filters[-1]["filter"]["stringFilter"]["value"]==path


@pytest.mark.parametrize("stage",["claim","finish"])
@pytest.mark.parametrize("change",["case_version","permission","resource","scope_snapshot","implementation"])
async def test_native_ga4_rechecks_authority_and_scope_at_both_boundaries(ga4_setup,stage,change):
    p=ga4_setup;job=await request(p)
    if stage=="finish":
        claimed=await p["collections"].claim_collection(job.id);result=await material(p,claimed)
    if change=="case_version": await p["db"].query("update client_cases set updated_at=clock_timestamp() where id=%s",[p["identity"]["case_id"]])
    elif change=="permission": await p["db"].query("update google_connections set granted_scopes='{}' where id=%s",[p["connection"]])
    elif change=="resource": await p["db"].query("update case_source_bindings set external_resource_id='properties/999' where id=%s",[p["binding"]])
    elif change=="implementation": await revise(p)
    else: await insert_snapshot(p["db"],p["source"].model_copy(update={"snapshot_id":uuid4(),"fetched_at":utc_now()}))
    if stage=="claim": assert await execute(p,job) is None and p["calls"]==[]
    else: assert (await p["collections"].finish_collection(claimed,result)).status=="failed"
    failed=await request(p,collection_id=job.id)
    assert failed.status=="failed" and failed.error_code=="SYNC_BINDING_CHANGED" and failed.collection is None
    assert await p["db"].query("select id from capability_ga4_collections")==[]
    assert await inspect(p["db"],p["scenario"])==p["baseline"]


async def test_native_ga4_lost_request_and_finish_ack_no_replay(ga4_setup):
    p=ga4_setup;cid=uuid4();lost=set()
    async def transport(req):
        answer=await p["db"].transport(req);name=req.url.path.rsplit("/",1)[-1]
        if name in ("request_v22_ga4_collection","finish_v22_ga4_collection") and answer.status_code==200 and name not in lost:
            lost.add(name);raise httpx.ReadTimeout("ack lost",request=req)
        return answer
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        p["collections"]=Ga4CollectionRepository(url="https://local-test.invalid",service_role_key="local",http_client=client)
        with pytest.raises(TransientJobError): await request(p,collection_id=cid)
        job=await request(p,collection_id=cid)
        with pytest.raises(TransientJobError): await execute(p,job)
        saved=await request(p,collection_id=cid)
        assert saved.status=="succeeded" and len(p["calls"])==8
        assert await execute(p,job) is None
        await p["db"].query("update google_connections set granted_scopes='{}' where id=%s",[p["connection"]])
        await revise(p)
        assert await request(p,collection_id=cid)==saved
        with pytest.raises(DeterministicJobError): await request(p)
    assert len(p["calls"])==8


@pytest.mark.parametrize("interruption",["cancel","lost_claim"])
async def test_native_ga4_interruption_expiry_fences_old_worker(ga4_setup,interruption):
    p=ga4_setup;job=await request(p);started=asyncio.Event()
    async def blocked(req):
        p["calls"].append(req);started.set();await asyncio.Event().wait()
    if interruption=="cancel":
        task=asyncio.create_task(execute(p,job,handler=blocked))
        await asyncio.wait_for(started.wait(),5);task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
    else:
        async def transport(req):
            answer=await p["db"].transport(req)
            if req.url.path.endswith("/claim_v22_ga4_collection") and answer.status_code==200:
                raise httpx.ReadTimeout("claim lost",request=req)
            return answer
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            repo=Ga4CollectionRepository(url="https://local-test.invalid",service_role_key="local",http_client=client)
            with pytest.raises(TransientJobError): await repo.claim_collection(job.id)
    running=await request(p,collection_id=job.id)
    assert running.status=="running" and await execute(p,job) is None
    await p["db"].query("update capability_ga4_collection_jobs set lease_expires_at=now()-interval '1 second' where id=%s",[job.id])
    assert await p["db"].rpc("pending_v22_ga4_collections",{})==[str(job.id)]
    assert await execute(p,job) is None
    failed=await request(p,collection_id=job.id)
    assert failed.status=="failed" and failed.error_code=="SYNC_INTERRUPTED"
    assert len(p["calls"])==(1 if interruption=="cancel" else 0)
    with pytest.raises(Exception,match="COLLECTION_LEASE_LOST"):
        await p["db"].rpc("finish_v22_ga4_collection",dict(p_id=str(job.id),p_lease_id=str(running.lease_id),
            p_payload={},p_canonical_payload="{}",p_canonical_snapshot="{}"))
    await p["collections"].fail_collection(running,"SYNC_FAILED")
    assert await request(p,collection_id=job.id)==failed


async def test_native_ga4_failure_no_partial_publish_or_secret(ga4_setup):
    p=ga4_setup;job=await request(p)
    def unavailable(req):
        if len(p["calls"])==3:
            p["calls"].append(req);return httpx.Response(503,json={"secret":"private-provider-detail"})
        return p["provider_handler"](req)
    failed=await execute(p,job,handler=unavailable)
    assert failed.status=="failed" and failed.error_code=="SYNC_GOOGLE_UNAVAILABLE" and failed.collection is None
    assert "private-provider-detail" not in failed.model_dump_json()
    assert await execute(p,job) is None and len(p["calls"])==4
    next_job=await request(p);claimed=await p["collections"].claim_collection(next_job.id)
    await p["collections"].fail_collection(claimed,"SYNC_SECRET_TOKEN_ABC")
    assert (await request(p,collection_id=next_job.id)).error_code=="SYNC_FAILED"
    assert await ordinary(p)==p["ordinary"]
    assert await inspect(p["db"],p["scenario"])==p["baseline"]


async def test_native_ga4_integrity_immutability_privileges(ga4_setup):
    p=ga4_setup;job=await request(p);claimed=await p["collections"].claim_collection(job.id);result=await material(p,claimed)
    for key,value in (("effect_conclusion","effective"),("causal_attribution",True),("processing_completeness","final")):
        forged=result.model_dump(mode="json",exclude={"checksum"});forged["qualification"][key]=value
        canonical=verified_canonical_json_bytes(forged).decode();forged["checksum"]=verified_request_digest(forged)
        with pytest.raises(Exception,match="COLLECTION_INVALID"):
            await p["db"].rpc("finish_v22_ga4_collection",dict(p_id=str(job.id),p_lease_id=str(claimed.lease_id),p_payload=forged,
                p_canonical_payload=canonical,p_canonical_snapshot=verified_canonical_json_bytes(forged["snapshot"]).decode()))
    saved=await p["collections"].finish_collection(claimed,result)
    assert await p["collections"].finish_collection(claimed,result)==saved
    with pytest.raises(DeterministicJobError): await p["collections"].finish_collection(claimed.model_copy(update={"lease_id":uuid4()}),result)
    with pytest.raises(Exception,match="IMMUTABLE"):
        await p["db"].query("update capability_ga4_collections set payload='{}' where id=%s",[job.id])
    rights=await p["db"].query("""select
      has_table_privilege('service_role','capability_ga4_collection_jobs','UPDATE') job_write,
      has_table_privilege('service_role','capability_ga4_collections','INSERT') material_write,
      has_function_privilege('authenticated','request_v22_ga4_collection(uuid,uuid,uuid,jsonb,jsonb,text)','EXECUTE') request,
      has_function_privilege('anon','get_v22_ga4_collection(uuid,uuid,uuid)','EXECUTE') read,
      has_function_privilege('authenticated','pending_v22_ga4_collections()','EXECUTE') pending,
      has_function_privilege('service_role','v22_ga4_lock_collection(uuid)','EXECUTE') helper""")
    assert not any(rights[0].values())


@pytest.mark.parametrize("change",["expired","host","timezone","checksum","unconfirmed","disconnected"])
async def test_native_ga4_rejects_untrusted_scope_before_creating_job(ga4_setup,change):
    p=ga4_setup
    if change=="unconfirmed":
        await p["db"].query("update case_source_bindings set confirmed_by_user_id=null,confirmed_at=null,identity_match_status='needs_confirmation' where id=%s",[p["binding"]])
    elif change=="disconnected":
        await p["db"].query("update case_source_bindings set is_active=false,disconnected_at=now() where id=%s",[p["binding"]])
    else:
        # Ordinary snapshots are immutable too. Insert a controlled latest source;
        # never weaken production guards to make a negative fixture possible.
        raw=p["source"].model_dump(mode="json")
        if change=="host": raw["normalized_payload"]["host_filter"]=["other.test"]
        elif change=="timezone": raw["normalized_payload"]["previous"]["totals"]["metadata"]["time_zone"]="UTC"
        resign_source(raw)
        source=TrustedFirstPartySnapshot.model_validate_json(json.dumps(raw))
        changes=dict(snapshot_id=uuid4(),fetched_at=utc_now())
        if change=="expired": changes["expires_at"]=utc_now()
        if change=="checksum": changes["payload_checksum"]="sha256:"+"0"*64
        source=source.model_copy(update=changes)
        await insert_snapshot(p["db"],source)
        p["choice"]=p["choice"].model_copy(update={"scope_snapshot_id":source.snapshot_id})
    with pytest.raises(DeterministicJobError): await request(p)
    assert await p["db"].query("select id from capability_ga4_collection_jobs")==[] and p["calls"]==[]


async def test_native_ga4_sql_rejects_malformed_selection_and_same_id_collision(ga4_setup):
    p=ga4_setup
    for path in ("https://example.test/a","//example.test/a","/a?secret=1","/a#b","/a b","/a\\b"):
        selection=p["choice"].model_dump(mode="json")|dict(landing_path=path)
        with pytest.raises(Exception,match="COLLECTION_INVALID"):
            await p["db"].rpc("resolve_v22_ga4_collection_input",dict(p_user_id=str(p["identity"]["user_id"]),
                p_case_id=str(p["identity"]["case_id"]),p_selection=selection))
    cid=uuid4()
    outcomes=await asyncio.gather(*(request(p,collection_id=cid,selection=p["choice"].model_copy(update={"days":days}))
        for days in (7,14,28)),return_exceptions=True)
    assert sum(not isinstance(o,Exception) for o in outcomes)==1
    assert all(isinstance(o,DeterministicJobError) for o in outcomes if isinstance(o,Exception))
    assert len(await p["db"].query("select id from capability_ga4_collection_jobs"))==1


@pytest.mark.parametrize("property_zone",["UTC","Asia/Shanghai"])
async def test_native_ga4_uses_observed_property_timezone_not_gsc_timezone(ga4_setup,property_zone):
    p=ga4_setup;raw=p["source"].model_dump(mode="json")
    for period in ("previous","current"):
        for view in ("totals","landing_pages","dates","key_events"):
            raw["normalized_payload"][period][view]["metadata"]["time_zone"]=property_zone
    resign_source(raw)
    source=TrustedFirstPartySnapshot.model_validate_json(json.dumps(raw)).model_copy(update={"snapshot_id":uuid4(),"fetched_at":utc_now()})
    await insert_snapshot(p["db"],source)
    p["choice"]=p["choice"].model_copy(update={"scope_snapshot_id":source.snapshot_id})
    job=await request(p)
    assert job.plan.scope.timezone==job.plan.windows.timezone==property_zone
    def response(req):
        answer=p["provider_handler"](req).json()
        if req.method=="POST": answer["metadata"]["timeZone"]=property_zone
        return httpx.Response(200,json=answer)
    assert (await execute(p,job,handler=response)).status=="succeeded"
