"""Real local SQL + Redis entry journey; all provider responses controlled."""
from uuid import UUID,uuid4
import httpx
import pytest
from arq.worker import Worker,func
from app.google_connections_v22.ga4_comparison import Ga4ComparisonProvider
from app.jobs_v22.cost_models import PricingCatalog
from app.jobs_v22.errors import DeterministicJobError
from app.report_v22.ga4_collection_entry import Ga4CollectionEntry,Ga4CollectionCommand
from app.report_v22.ga4_collection_worker import reconcile_v22_ga4_collections,execute_v22_ga4_collection
from tests.integration.test_native_ga4_collection_storage import ga4_setup,prepared,native_database,ordinary,inspect

pytestmark=[pytest.mark.native_integration,pytest.mark.anyio,pytest.mark.timeout(60)]
def command(p,**changes):
    return Ga4CollectionCommand(**(dict(operation="context",**p["identity"],report_id=UUID(p["record"]["report_id"]),
        delivery_revision_id=UUID(p["record"]["delivery_revision_id"]),collection_id=None,selection=None)|changes))

async def test_native_ga4_entry_dispatch_and_safe_projection(ga4_setup,arq_pool,redis_prefix,monkeypatch):
    p=ga4_setup;entry=Ga4CollectionEntry(p["collections"])
    from app.api.v2.ga4_collections import collection_service
    from app.core.config import settings
    from app.main import create_app
    for flag in ("V22_GA4_COMPARISON_COLLECTIONS_ENABLED","V22_COMPARISONS_ENABLED","V22_CAPABILITY_EXECUTION_ENABLED"):
        monkeypatch.setattr(settings,flag,True)
    monkeypatch.setattr(settings,"V22_INTERNAL_API_TOKEN","native-test")
    app=create_app();app.dependency_overrides[collection_service]=lambda:entry
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as client:
        async def call(c):return await client.post("/api/v2/ga4-comparison-collections",json=c.model_dump(mode="json"),headers={"Authorization":"Bearer native-test"})
        response=await call(command(p));assert response.status_code==200,response.text
        context=response.json();assert len(context["previews"])==4 and context["jobs"]==[]
        assert context["bindings"][0]["scope"]["hosts"]==[p["domain"]]
        assert p["calls"]==[] and await p["db"].rpc("pending_v22_ga4_collections",{})==[]
        ask=command(p,operation="request",collection_id=uuid4(),selection=p["choice"])
        result=await call(ask);assert result.status_code==200,result.text
        assert result.json()["status"]=="queued" and (await call(ask)).json()==result.json()
        assert (await call(ask.model_copy(update={"report_id":uuid4()}))).status_code==404
        class Broker:
            async def access_token(self,connection):return "test-only"
        async with httpx.AsyncClient(transport=httpx.MockTransport(p["provider_handler"])) as provider_client:
            ctx=dict(redis=arq_pool,ga4_comparison_collection_repository=p["collections"],ga4_comparison_collection_queue=f"{redis_prefix}:ga4",
                ga4_comparison_collection_provider=Ga4ComparisonProvider(provider_client),ga4_comparison_collection_broker=Broker(),
                cost_pricing=PricingCatalog(),cost_prefix=redis_prefix,state_ttl_seconds=600)
            await reconcile_v22_ga4_collections(ctx);await reconcile_v22_ga4_collections(ctx)
            worker=Worker([func(execute_v22_ga4_collection,max_tries=1,keep_result=0)],redis_pool=arq_pool,
                queue_name=ctx["ga4_comparison_collection_queue"],ctx=ctx,burst=True,handle_signals=False,poll_delay=.01)
            try:await worker.async_run()
            finally:await worker.close()
        done=(await call(command(p))).json()["jobs"][0]
        assert done["status"]=="succeeded" and len(p["calls"])==8
        assert done["qualification"]["effect_conclusion"] is None
        assert all(m["state"]=="not_comparable" for m in done["qualification"]["metrics"])
        assert "normalized_payload" not in str(done) and "snapshot" not in done and "user_id" not in done
        assert (await call(ask)).json()==done and len(p["calls"])==8
    assert await ordinary(p)==p["ordinary"] and await inspect(p["db"],p["scenario"])==p["baseline"]

async def test_native_ga4_entry_waiting_permissions_and_identifier_collision(ga4_setup):
    p=ga4_setup;entry=Ga4CollectionEntry(p["collections"])
    with pytest.raises(DeterministicJobError):await entry.run(command(p,user_id=uuid4()))
    with pytest.raises(DeterministicJobError,match="WAITING"):
        await entry.run(command(p,operation="request",collection_id=uuid4(),selection=p["choice"].model_copy(update={"days":90})))
    with pytest.raises(DeterministicJobError):
        await entry.run(command(p,operation="request",collection_id=p["source"].snapshot_id,selection=p["choice"]))
    analysis=(await p["db"].query("select id from analysis_jobs where case_id=%s limit 1",[p["identity"]["case_id"]]))[0]["id"]
    with pytest.raises(DeterministicJobError):
        await entry.run(command(p,operation="request",collection_id=analysis,selection=p["choice"]))
    assert await p["db"].rpc("pending_v22_ga4_collections",{})==[] and p["calls"]==[]
    rights=await p["db"].query("select has_function_privilege('authenticated','get_v22_ga4_collection_context(uuid,uuid,uuid,uuid)','EXECUTE') allowed")
    assert not rights[0]["allowed"]
    await p["db"].query("update google_connections set status='error' where id=%s",[p["connection"]])
    assert (await entry.run(command(p))).bindings==[]

async def test_native_ga4_id_cannot_shadow_gsc_or_analysis_job(ga4_setup):
    p=ga4_setup;entry=Ga4CollectionEntry(p["collections"])
    from app.report_v22.collection_storage import CollectionRepository,CollectionSelection
    async with httpx.AsyncClient(transport=httpx.MockTransport(p["db"].transport)) as client:
        gsc=CollectionRepository(url="https://local-test.invalid",service_role_key="local",http_client=client)
        selection=CollectionSelection(implementation=p["choice"].implementation,action_id=p["choice"].action_id,
            days=28,binding_id=p["sources"][0].binding_id)
        first=await gsc.request_collection(**p["identity"],collection_id=uuid4(),selection=selection)
        claimed=await gsc.claim_collection(first.id)
        await gsc.fail_collection(claimed,"SYNC_INTERRUPTED")
        with pytest.raises(DeterministicJobError):
            await entry.run(command(p,operation="request",collection_id=first.id,selection=p["choice"]))
        second=await entry.run(command(p,operation="request",collection_id=uuid4(),selection=p["choice"]))
        with pytest.raises(DeterministicJobError):
            await gsc.request_collection(**p["identity"],collection_id=second.id,selection=selection)
        with pytest.raises(Exception,match="COLLECTION_ID_CONFLICT"):
            await p["db"].query("insert into analysis_jobs(id,case_id,job_type,current_stage,idempotency_key) values(%s,%s,'verified_report','queued',%s)",
                [second.id,p["identity"]["case_id"],str(uuid4())])
        from tests.integration.test_native_comparison_storage import insert_snapshot
        with pytest.raises(Exception,match="COLLECTION_ID_CONFLICT"):
            await insert_snapshot(p["db"],p["source"].model_copy(update={"snapshot_id":second.id}))
