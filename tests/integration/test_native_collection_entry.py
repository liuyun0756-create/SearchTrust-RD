"""Actual entry, SQL, Redis dispatch and comparison resolution; providers controlled."""
import json
from uuid import UUID, uuid4

import httpx
import pytest
from arq.worker import Worker, func

from app.google_connections_v22.gsc import GscProvider
from app.jobs_v22.cost_models import PricingCatalog
from app.jobs_v22.errors import DeterministicJobError
from app.report_v22.collection_entry import CollectionCommand, CollectionEntry, CollectionContext, CollectionView
from app.report_v22.collection_worker import reconcile_v22_collections, execute_v22_collection
from app.report_v22.comparison_entry import ComparisonEntry, EntryCommand
from tests.integration.test_native_collection_storage import collection_setup, prepared, native_database, request, execute, ordinary, inspect

pytestmark=[pytest.mark.native_integration,pytest.mark.anyio,pytest.mark.timeout(60)]

def command(p,**changes):
    return CollectionCommand(**(dict(operation="context",**p["identity"],report_id=UUID(p["record"]["report_id"]),
        delivery_revision_id=UUID(p["record"]["delivery_revision_id"]),collection_id=None,selection=None)|changes))


async def test_native_owner_collection_route_dispatch_to_saved_comparison(collection_setup,arq_pool,redis_prefix,monkeypatch):
    p=collection_setup;entry=CollectionEntry(p["collections"])
    from app.api.v2.collections import collection_service
    from app.core.config import settings
    from app.main import create_app
    for name in ("V22_COMPARISON_COLLECTIONS_ENABLED","V22_COMPARISONS_ENABLED","V22_CAPABILITY_EXECUTION_ENABLED"):
        monkeypatch.setattr(settings,name,True)
    monkeypatch.setattr(settings,"V22_INTERNAL_API_TOKEN","native-test")
    app=create_app();app.dependency_overrides[collection_service]=lambda:entry
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as client:
        async def call(c):return await client.post("/api/v2/comparison-collections",json=c.model_dump(mode="json"),headers={"Authorization":"Bearer native-test"})
        c=command(p);response=await call(c)
        assert response.status_code==200
        context=CollectionContext.model_validate_json(response.content)
        assert len(context.previews)==4 and context.bindings[0].id==p["binding"] and context.jobs==[]
        assert await p["db"].rpc("pending_v22_collections",{})==[]
        ask=command(p,operation="request",collection_id=uuid4(),selection=p["choice"])
        result=await call(ask);assert result.status_code==200
        queued=CollectionView.model_validate_json(result.content)
        assert queued.status=="queued" and "snapshot" not in result.text and "user_id" not in result.text
        assert (await call(ask)).json()==result.json()
        assert (await call(ask.model_copy(update={"report_id":uuid4()}))).status_code==404
        assert len((await call(c)).json()["jobs"])==1
        class Broker:
            async def access_token(self,connection):return "test-only"
        async with httpx.AsyncClient(transport=httpx.MockTransport(p["provider_handler"])) as provider_client:
            ctx=dict(redis=arq_pool,comparison_collection_repository=p["collections"],comparison_collection_queue=f"{redis_prefix}:collection",
                comparison_collection_provider=GscProvider(provider_client),comparison_collection_broker=Broker(),
                cost_pricing=PricingCatalog(),cost_prefix=redis_prefix,state_ttl_seconds=600)
            await reconcile_v22_collections(ctx)
            worker=Worker([func(execute_v22_collection,max_tries=1,keep_result=0)],redis_pool=arq_pool,
                queue_name=ctx["comparison_collection_queue"],ctx=ctx,burst=True,handle_signals=False,poll_delay=.01)
            try:await worker.async_run()
            finally:await worker.close()
        done=(await call(c)).json()["jobs"][0]
        assert done["status"]=="succeeded" and len(p["calls"])==12
    checks=ComparisonEntry(p["repo"])
    base=EntryCommand(operation="context",**p["identity"],report_id=UUID(p["record"]["report_id"]),
        delivery_revision_id=UUID(p["record"]["delivery_revision_id"]),comparison_id=None,selection=None)
    catalogue=await checks.run(base)
    assert next(s for s in catalogue.sources if s.id==queued.id).current_eligible
    selection=p["selection"].model_copy(update={"before_snapshot_id":queued.id,"after_snapshot_id":queued.id})
    check=base.model_copy(update={"operation":"freeze","comparison_id":uuid4(),"selection":selection})
    saved=await checks.run(check)
    assert saved.result.state=="comparable" and saved.result.effect_conclusion is None and not saved.result.causal_attribution
    assert saved.result.before_source.snapshot_id==queued.id==saved.result.after_source.snapshot_id
    assert await checks.run(check)==saved
    assert await ordinary(p)==p["ordinary"] and await inspect(p["db"],p["scenario"])==p["baseline"]


async def test_native_collected_source_rejects_wrong_purpose_expiry_and_revocation(collection_setup):
    p=collection_setup;job=await request(p);await execute(p,job)
    selected=p["selection"].model_copy(update={"before_snapshot_id":job.id,"after_snapshot_id":job.id})
    for updates in ({"days":7},{"action_id":"wrong-action"},{"before_snapshot_id":None},
                    {"after_snapshot_id":p["sources"][1].snapshot_id}):
        with pytest.raises(DeterministicJobError):await p["repo"].freeze(**p["identity"],comparison_id=uuid4(),selection=selected.model_copy(update=updates))
    saved=await p["repo"].freeze(**p["identity"],comparison_id=uuid4(),selection=selected)
    # Simulate expiration at the future evaluation instant without mutating immutable material.
    from app.jobs_v22.models import utc_now
    from datetime import timedelta
    evaluated=utc_now()+timedelta(days=8)
    with pytest.raises(Exception,match="COMPARISON_SOURCE_CHANGED"):
        await p["db"].query("select v22_comparison_snapshot(%s,%s,%s,'gsc',true,%s)",
            [p["identity"]["user_id"],p["identity"]["case_id"],job.id,evaluated])
    await p["db"].query("update google_connections set status='error' where id=%s",[p["connection"]])
    assert await p["repo"].read(**p["identity"],comparison_id=saved.id)==saved
    with pytest.raises(DeterministicJobError):await p["repo"].freeze(**p["identity"],comparison_id=uuid4(),selection=selected)
    rights=await p["db"].query("""select
      has_function_privilege('authenticated','get_v22_collection_context(uuid,uuid,uuid,uuid)','EXECUTE') context,
      has_function_privilege('authenticated','pending_v22_collections()','EXECUTE') pending,
      has_function_privilege('service_role','resolve_v22_ordinary_comparison_input(uuid,uuid,jsonb,timestamptz)','EXECUTE') bypass""")
    assert not any(rights[0].values())


async def test_native_collection_read_only_routes_cannot_start_work(collection_setup,monkeypatch):
    p=collection_setup;entry=CollectionEntry(p["collections"])
    await entry.run(command(p));await entry.run(command(p))
    assert await p["db"].rpc("pending_v22_collections",{})==[] and p["calls"]==[]
    with pytest.raises(DeterministicJobError):await entry.run(command(p,user_id=uuid4()))
    with pytest.raises(DeterministicJobError):await entry.run(command(p,operation="read",collection_id=uuid4()))
    with pytest.raises(DeterministicJobError,match="WAITING"):
        await entry.run(command(p,operation="request",collection_id=uuid4(),selection=p["choice"].model_copy(update={"days":90})))
    assert await p["db"].rpc("pending_v22_collections",{})==[]
    with pytest.raises(DeterministicJobError):await request(p,collection_id=p["sources"][0].snapshot_id)
    assert await p["db"].rpc("pending_v22_collections",{})==[]
