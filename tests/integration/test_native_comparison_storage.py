"""Real entitlement, SQL freeze/locks and backend replay, with synthetic sources."""
import asyncio
from copy import deepcopy
from datetime import timedelta
import json
from uuid import uuid4

import httpx
import pytest

from app.jobs_v22.digest import verified_canonical_json_bytes, verified_request_digest, canonical_json_bytes
from app.jobs_v22.models import utc_now
from app.jobs_v22.first_service_preparation_reconciler import reconcile_v22_first_service_preparation
from app.report_v22.comparison import compare_implementation
from app.report_v22.comparison_storage import ComparisonRepository, ComparisonSelection
from app.report_v22.comparison_entry import ComparisonEntry, EntryCommand, ComparisonView
from app.jobs_v22.errors import DeterministicJobError, TransientJobError
from support.comparison_fixtures import saved_gsc, resign_source
from tests.integration.test_native_first_service_recovery import (
    native_database, worker, wait_for, assert_completed, inspect, CONFIRM,
)
from tests.integration.support.native_capability import seed_first_service
from tests.integration.support.native_worker import make_context

pytestmark = [pytest.mark.native_integration, pytest.mark.anyio, pytest.mark.timeout(50)]


async def insert_snapshot(db, source):
    await db.query("""insert into data_snapshots(id,case_id,binding_id,source_type,schema_version,fetched_at,expires_at,
      sync_trigger,health_status,health_reasons,normalized_payload,payload_checksum,coverage_start,coverage_end,provider_request_context)
      values(%s,%s,%s,%s,%s,%s,%s,'user_sync',%s,%s::jsonb,%s::jsonb,%s,%s,%s,%s::jsonb)""",
      [source.snapshot_id, source.case_id, source.binding_id, source.source_type, source.schema_version,
       source.fetched_at, source.expires_at, source.health_status, json.dumps(source.health_reasons),
       canonical_json_bytes(source.normalized_payload).decode(), source.payload_checksum, source.coverage_start, source.coverage_end,
       json.dumps(dict(external_resource_id=source.external_resource_id))])


@pytest.fixture
async def prepared(native_database, arq_pool, redis_prefix):
    db = native_database
    scenario = await seed_first_service(db, arq_pool, redis_prefix)
    await db.query(CONFIRM, scenario["confirm"], True)
    ctx = await make_context(db, arq_pool, redis_prefix)
    try:
        await reconcile_v22_first_service_preparation(ctx)
        with worker(db, redis_prefix) as (process, diagnostics):
            async def completed():
                state = await ctx["store"].get_state(scenario["job"])
                return state and state.status == "succeeded"
            await wait_for(completed, process, diagnostics)
        baseline = await assert_completed(db, scenario, arq_pool, redis_prefix)
        report = baseline["reports"][0]["payload"]
        delivery = dict(schema_version="v22_delivery_revision_v1", report_id=report["id"], user_id=str(scenario["owner"]),
            case_id=str(scenario["case"]), expected_revision=0, source_checksum=verified_request_digest(report), status="approved",
            title=report["title"], summary=report["summary"], client_note=None,
            actions=[{k:v for k,v in action.items() if k not in {"finding_ids", "dependencies"}} for action in report["actions"]])
        approved = (await db.query("select * from save_v22_completed_report_revision(%s,%s,%s,%s::jsonb)",
            [scenario["owner"], scenario["case"], report["id"], json.dumps(delivery)], True))[0]
        now = utc_now()
        record = dict(schema_version="v22_implementation_record_v1", id=str(uuid4()), user_id=str(scenario["owner"]),
            case_id=str(scenario["case"]), report_id=report["id"], report_checksum=delivery["source_checksum"],
            delivery_revision_id=str(approved["id"]), delivery_revision=1, revision=1,
            recorded_at=now.isoformat().replace("+00:00", "Z"), entries=[dict(action_id=delivery["actions"][0]["id"],
                scope=delivery["actions"][0]["target"], implemented_on=(now-timedelta(days=60)).date().isoformat(),
                timezone="America/Los_Angeles", note=None, responsible=None)])
        canonical = verified_canonical_json_bytes(record).decode()
        record["checksum"] = verified_request_digest(record)
        await db.query("select save_v22_implementation_record(%s,%s,%s,%s,0,%s::jsonb,%s)",
            [scenario["owner"], scenario["case"], report["id"], approved["id"], json.dumps(record), canonical], True)
        old = (await db.query("select * from case_source_bindings where case_id=%s and source_type='gsc'", [scenario["case"]]))[0]
        await db.query("update case_source_bindings set is_active=false,disconnected_at=now() where id=%s", [old["id"]])
        binding = uuid4()
        await db.query("""insert into case_source_bindings(id,case_id,connection_id,source_type,external_resource_id,external_resource_name,
          identity_match_status,health_status,health_reasons,confirmed_by_user_id,confirmed_at)
          values(%s,%s,%s,'gsc','sc-domain:example.test','Comparison test','matched','healthy','[]',%s,now()-interval '2 days')""",
          [binding, scenario["case"], old["connection_id"], scenario["owner"]])
        sources = []
        for current in (False, True):
            source = saved_gsc(scenario["case"], after=current, end=(now-timedelta(days=3 if current else 61)).date())
            source = source.model_copy(update=dict(snapshot_id=uuid4(), binding_id=binding,
                fetched_at=now-timedelta(minutes=1) if current else now-timedelta(days=59),
                expires_at=now+timedelta(days=3) if current else now-timedelta(days=52)))
            await insert_snapshot(db, source)
            sources.append(source)
        # Source health reasons are provider-derived and must be exact on current binding.
        await db.query("update case_source_bindings set health_reasons=%s::jsonb where id=%s", [json.dumps(sources[1].health_reasons), binding])
        selection = ComparisonSelection.model_validate_json(json.dumps(dict(implementation={k:record[k] for k in ("id", "revision", "checksum")},
            action_id=record["entries"][0]["action_id"], days=28,
            scope=dict(source="gsc", dimension="property", key=None, channel="organic_search", filters=[]),
            before_snapshot_id=str(sources[0].snapshot_id), after_snapshot_id=str(sources[1].snapshot_id), concurrent_changes="unknown")))
        from app.report_v22.comparison_models import ComparisonRequest
        resolved = await db.rpc("resolve_v22_comparison_input",dict(p_user_id=str(scenario["owner"]),p_case_id=str(scenario["case"]),
            p_selection=selection.model_dump(mode="json"),p_evaluated_at=utc_now().isoformat()))
        compare_implementation(ComparisonRequest.model_validate_json(json.dumps(resolved)))
        async with httpx.AsyncClient(transport=httpx.MockTransport(db.transport)) as client:
            repo = ComparisonRepository(url="https://local-test.invalid", service_role_key="local", http_client=client)
            yield dict(db=db, repo=repo, scenario=scenario, baseline=baseline, selection=selection, record=record,
                binding=binding, connection=old["connection_id"], sources=sources,
                identity=dict(user_id=scenario["owner"], case_id=scenario["case"]))
    finally:
        await ctx["native_client"].aclose()


async def raw_freeze(p, request, *, comparison_id=None, result=None):
    result = result or compare_implementation(request).model_dump(mode="json")
    return await p["db"].rpc("freeze_v22_comparison", dict(p_user_id=str(p["identity"]["user_id"]), p_case_id=str(p["identity"]["case_id"]),
        p_id=str(comparison_id or uuid4()), p_selection=p["selection"].model_dump(mode="json"), p_request=request.model_dump(mode="json"),
        p_canonical_request=verified_canonical_json_bytes(request.model_dump(mode="json")).decode(), p_result=result,
        p_canonical_result=verified_canonical_json_bytes({k:v for k,v in result.items() if k != "checksum"}).decode()))


async def test_native_owner_entry_catalogue_freeze_history_and_revocation(prepared, monkeypatch):
    p=prepared; entry=ComparisonEntry(p["repo"])
    command=EntryCommand.model_validate_json(json.dumps(dict(operation="context",**{k:str(v) for k,v in p["identity"].items()},
        report_id=p["record"]["report_id"],delivery_revision_id=p["record"]["delivery_revision_id"],comparison_id=None,selection=None)))
    catalogue=await entry.run(command)
    assert catalogue.implementation==p["selection"].implementation and catalogue.history==[]
    source_map={s.id:s for s in catalogue.sources}
    assert not source_map[p["sources"][0].snapshot_id].current_eligible
    assert source_map[p["sources"][1].snapshot_id].current_eligible
    assert "normalized_payload" not in catalogue.model_dump_json()
    check=command.model_copy(update=dict(operation="freeze",comparison_id=uuid4(),selection=p["selection"]))
    from app.api.v2.comparisons import comparison_service
    from app.core.config import settings
    from app.main import create_app
    monkeypatch.setattr(settings,"V22_COMPARISONS_ENABLED",True)
    monkeypatch.setattr(settings,"V22_CAPABILITY_EXECUTION_ENABLED",True)
    monkeypatch.setattr(settings,"V22_INTERNAL_API_TOKEN","native-test")
    app=create_app(); app.dependency_overrides[comparison_service]=lambda:entry
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as client:
        response=await client.post("/api/v2/comparisons",json=check.model_dump(mode="json"),headers={"Authorization":"Bearer native-test"})
    assert response.status_code==200
    saved=ComparisonView.model_validate_json(response.content)
    assert saved.result.state=="comparable" and saved.result.effect_conclusion is None
    assert set(saved.model_dump())=={"schema_version","id","case_id","report_id","delivery_revision_id","result"}
    catalogue=await entry.run(command)
    assert [h.id for h in catalogue.history]==[saved.id]
    assert await entry.run(check)==saved
    with pytest.raises(DeterministicJobError):
        await entry.run(check.model_copy(update={"report_id":uuid4()}))
    with pytest.raises(DeterministicJobError):
        await entry.run(command.model_copy(update={"user_id":uuid4()}))
    stale=p["selection"].model_copy(update={"implementation":p["selection"].implementation.model_copy(update={"revision":2})})
    with pytest.raises(DeterministicJobError,match="CONFLICT"):
        await entry.run(check.model_copy(update={"comparison_id":uuid4(),"selection":stale}))
    await p["db"].query("update google_connections set granted_scopes='{}' where id=%s",[p["connection"]])
    assert (await entry.run(command)).sources==[]
    assert await entry.run(check)==saved
    assert await entry.run(check.model_copy(update={"operation":"read","selection":None}))==saved
    with pytest.raises(DeterministicJobError,match="CONFLICT"):
        await entry.run(check.model_copy(update={"comparison_id":uuid4()}))
    privileges=await p["db"].query("select has_function_privilege('authenticated','public.get_v22_comparison_context(uuid,uuid,uuid,uuid)','EXECUTE') allowed")
    assert privileges==[{"allowed":False}]
    assert await inspect(p["db"],p["scenario"])==p["baseline"]


async def test_native_comparison_concurrent_free_retry_and_frozen_history(prepared):
    p = prepared
    comparison_id = uuid4()
    results = await asyncio.gather(*(p["repo"].freeze(**p["identity"], comparison_id=comparison_id, selection=p["selection"]) for _ in range(6)))
    assert all(r == results[0] for r in results)
    first = results[0]
    assert first.result.state == "comparable" and first.result.effect_conclusion is None
    assert first.request.before.expires_at < utc_now()  # expired baseline remains usable
    assert len(await p["db"].query("select id from capability_comparisons")) == 1
    with pytest.raises(DeterministicJobError, match="CONFLICT"):
        await p["repo"].freeze(**p["identity"], comparison_id=comparison_id, selection=p["selection"].model_copy(update={"days":7}))
    other = p["selection"].model_copy(update={"days":7})
    args = dict(p_user_id=str(p["identity"]["user_id"]), p_case_id=str(p["identity"]["case_id"]), p_id=str(comparison_id),
        p_selection=other.model_dump(mode="json"), p_request={}, p_canonical_request="{}", p_result={}, p_canonical_result="{}")
    with pytest.raises(Exception, match="COMPARISON_CONFLICT"): await p["db"].rpc("freeze_v22_comparison", args)
    correction = deepcopy(p["record"])
    correction.pop("checksum")
    correction.update(revision=2, recorded_at=utc_now().isoformat().replace("+00:00", "Z"))
    correction["entries"][0]["implemented_on"] = (utc_now()-timedelta(days=45)).date().isoformat()
    canonical = verified_canonical_json_bytes(correction).decode()
    correction["checksum"] = verified_request_digest(correction)
    await p["db"].query("select save_v22_implementation_record(%s,%s,%s,%s,1,%s::jsonb,%s)",
        [p["identity"]["user_id"],p["identity"]["case_id"],correction["report_id"],correction["delivery_revision_id"],json.dumps(correction),canonical], True)
    await p["db"].query("update google_connections set granted_scopes='{}' where id=%s", [p["connection"]])
    assert await p["repo"].read(**p["identity"], comparison_id=comparison_id) == first
    assert await p["repo"].freeze(**p["identity"], comparison_id=comparison_id, selection=p["selection"]) == first
    with pytest.raises(DeterministicJobError, match="REJECTED"):
        await p["repo"].freeze(**p["identity"], comparison_id=uuid4(), selection=p["selection"])
    with pytest.raises(Exception, match="IMMUTABLE"):
        await p["db"].query("update capability_comparisons set result='{}' where id=%s", [comparison_id])
    with pytest.raises(Exception, match="permission denied"):
        await p["db"].query("delete from capability_comparisons where id=%s", [comparison_id], True)
    assert await inspect(p["db"], p["scenario"]) == p["baseline"]


async def test_native_comparison_rejects_owner_selection_and_permission_races(prepared):
    p = prepared
    selection = p["selection"]
    request = await p["repo"].resolve(**p["identity"], selection=selection)
    for identity in (dict(user_id=uuid4(),case_id=p["identity"]["case_id"]),dict(user_id=p["identity"]["user_id"],case_id=uuid4())):
        with pytest.raises(DeterministicJobError, match="REJECTED"):
            await p["repo"].resolve(**identity, selection=selection)
    for changes in ({"action_id":"not-an-approved-action"}, {"before_snapshot_id":uuid4()},
                    {"implementation":selection.implementation.model_copy(update={"revision":2})}):
        with pytest.raises(DeterministicJobError, match="REJECTED"):
            await p["repo"].resolve(**p["identity"], selection=selection.model_copy(update=changes))
    # Permission can disappear after calculation but before persistence.
    await p["db"].query("update google_connections set status='error' where id=%s", [p["connection"]])
    with pytest.raises(Exception, match="COMPARISON_SOURCE_INVALID"): await raw_freeze(p, request)
    await p["db"].query("update google_connections set status='active' where id=%s", [p["connection"]])
    await p["db"].query("update case_source_bindings set identity_match_status='needs_confirmation' where id=%s", [p["binding"]])
    with pytest.raises(Exception, match="COMPARISON_SOURCE_INVALID"): await raw_freeze(p, request)
    await p["db"].query("update case_source_bindings set identity_match_status='matched' where id=%s", [p["binding"]])
    # New collection supersedes current data between read and freeze.
    newer = p["sources"][1].model_copy(update={"snapshot_id":uuid4(), "fetched_at":utc_now()})
    await insert_snapshot(p["db"], newer)
    with pytest.raises(Exception, match="COMPARISON_SOURCE_CHANGED"): await raw_freeze(p, request)
    assert await p["db"].query("select id from capability_comparisons") == []
    assert await inspect(p["db"], p["scenario"]) == p["baseline"]


async def test_native_comparison_tamper_missing_and_ack_loss(prepared):
    p = prepared
    request = await p["repo"].resolve(**p["identity"], selection=p["selection"])
    result = compare_implementation(request).model_dump(mode="json")
    for key, value in (("effect_conclusion", "effective"),("causal_attribution", True),("input_checksum", "sha256:"+"0"*64)):
        forged = {**result, key:value}
        forged["checksum"] = verified_request_digest({k:v for k,v in forged.items() if k != "checksum"})
        with pytest.raises(Exception, match="COMPARISON_INVALID"): await raw_freeze(p, request, result=forged)
    forged = request.model_copy(deep=True)
    forged.before.normalized_payload["resource_id"] = "sc-domain:wrong.test"
    with pytest.raises(Exception, match="COMPARISON_SOURCE_CHANGED"): await raw_freeze(p, forged, result=result)
    selection = p["selection"].model_copy(update={"before_snapshot_id":None,"after_snapshot_id":None})
    comparison_id = uuid4()
    lost = False
    async def transport(req):
        nonlocal lost
        response = await p["db"].transport(req)
        if req.url.path.endswith("/freeze_v22_comparison") and response.status_code == 200 and not lost:
            lost = True
            raise httpx.ReadTimeout("simulated lost acknowledgement", request=req)
        return response
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        repo = ComparisonRepository(url="https://local-test.invalid", service_role_key="local", http_client=client)
        with pytest.raises(TransientJobError): await repo.freeze(**p["identity"], comparison_id=comparison_id, selection=selection)
        saved = await repo.freeze(**p["identity"], comparison_id=comparison_id, selection=selection)
        assert saved.result.state == "insufficient" and saved.result.metrics == []
        assert saved.request.before is None and saved.request.after is None
    assert len(await p["db"].query("select id from capability_comparisons")) == 1
    assert await inspect(p["db"], p["scenario"]) == p["baseline"]


async def test_native_comparison_selection_race_expiry_and_role_boundaries(prepared):
    p = prepared
    comparison_id = uuid4()
    selections = [p["selection"].model_copy(update={"days":days}) for days in (7,14,28)]
    attempts = await asyncio.gather(*(p["repo"].freeze(**p["identity"], comparison_id=comparison_id, selection=s) for s in selections), return_exceptions=True)
    successes = [r for r in attempts if not isinstance(r, Exception)]
    assert len(successes) == 1
    assert all(isinstance(r, DeterministicJobError) for r in attempts if isinstance(r, Exception))
    assert len(await p["db"].query("select id from capability_comparisons")) == 1
    permissions = await p["db"].query("""select
      has_function_privilege('authenticated','public.resolve_v22_comparison_input(uuid,uuid,jsonb,timestamptz)','EXECUTE') resolving,
      has_function_privilege('anon','public.get_v22_comparison(uuid,uuid,uuid)','EXECUTE') reading,
      has_function_privilege('authenticated','public.freeze_v22_comparison(uuid,uuid,uuid,jsonb,jsonb,text,jsonb,text)','EXECUTE') freezing,
      has_function_privilege('service_role','public.v22_comparison_snapshot(uuid,uuid,uuid,text,boolean,timestamptz)','EXECUTE') helper,
      has_table_privilege('service_role','public.capability_comparisons','INSERT') direct_write""")
    assert not any(permissions[0].values())
    assert await p["repo"].read(**p["identity"], comparison_id=uuid4()) is None
    with pytest.raises(DeterministicJobError, match="REJECTED"):
        await p["repo"].read(user_id=uuid4(), case_id=p["identity"]["case_id"], comparison_id=comparison_id)
    foreign_case = uuid4()
    await p["db"].query("""insert into client_cases(id,user_id,site_url,normalized_domain,business_name,business_identity,operating_model,primary_service,target_market)
      select %s,user_id,site_url,normalized_domain,business_name,business_identity-'primary_location',operating_model,primary_service,target_market
      from client_cases where id=%s""", [foreign_case,p["identity"]["case_id"]])
    assert await p["repo"].read(user_id=p["identity"]["user_id"], case_id=foreign_case, comparison_id=comparison_id) is None
    with pytest.raises(DeterministicJobError, match="REJECTED"):
        await p["repo"].resolve(user_id=p["identity"]["user_id"], case_id=foreign_case, selection=p["selection"])
    expired = p["sources"][1].model_copy(update={"snapshot_id":uuid4(), "fetched_at":utc_now()-timedelta(seconds=10),
        "expires_at":utc_now()-timedelta(seconds=1)})
    await insert_snapshot(p["db"], expired)
    with pytest.raises(DeterministicJobError, match="REJECTED"):
        await p["repo"].resolve(**p["identity"], selection=p["selection"].model_copy(update={"after_snapshot_id":expired.snapshot_id}))
    # Historic inspection uses its frozen values, independent of current freshness.
    assert await p["repo"].read(**p["identity"], comparison_id=comparison_id) == successes[0]
    assert await inspect(p["db"], p["scenario"]) == p["baseline"]


async def test_native_comparison_preserves_insufficient_and_incompatible_observations(prepared):
    from app.report_v22.comparison_models import ComparisonScope
    from app.report_v22.first_party_findings_models import TrustedFirstPartySnapshot
    p = prepared
    scope = ComparisonScope(source="gsc", dimension="landing_page", key="/service", channel="organic_search", filters=[])
    incompatible = await p["repo"].freeze(**p["identity"], comparison_id=uuid4(), selection=p["selection"].model_copy(update={"scope":scope}))
    assert incompatible.result.state == "not_comparable" and incompatible.result.metrics == []
    raw = p["sources"][1].model_dump(mode="json")
    raw.update(snapshot_id=str(uuid4()), fetched_at=utc_now().isoformat())
    raw["normalized_payload"]["current"]["totals"]["rows"] = []
    resign_source(raw)
    unhealthy = TrustedFirstPartySnapshot.model_validate_json(json.dumps(raw))
    assert unhealthy.health_status == "unhealthy"
    await insert_snapshot(p["db"], unhealthy)
    await p["db"].query("update case_source_bindings set health_status='unhealthy',health_reasons=%s::jsonb where id=%s",
        [json.dumps(unhealthy.health_reasons),p["binding"]])
    saved = await p["repo"].freeze(**p["identity"], comparison_id=uuid4(),
        selection=p["selection"].model_copy(update={"after_snapshot_id":unhealthy.snapshot_id}))
    assert saved.result.state == "insufficient" and saved.result.reasons == ["SOURCE_UNHEALTHY"]
    assert saved.result.metrics == [] and saved.result.effect_conclusion is None
    assert await inspect(p["db"], p["scenario"]) == p["baseline"]
