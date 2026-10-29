from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest

from app.api.v2.models import AnalyzeRequest
from app.competitors_v22.models import SharedMarketSnapshot
from app.competitors_v22.selection import AnalysisDiscoveryLink, AnalysisRequestEnvelope, analysis_discovery_input_digest
from app.jobs_v22.capability_acquisition_input import build_acquisition_input, stamp
from app.jobs_v22.capability_acquisition_preparation import CapabilityAcquisitionPreparer, begin_acquisition_preparation
from app.jobs_v22.capability_input_repository import SupabaseCapabilityInputRepository
from app.jobs_v22.capability_inputs import generation_recipe
from app.jobs_v22.customer_public_gbp_stage import CustomerPublicGbpCollection
from app.jobs_v22.cost_models import ALLOWED_COUNTER_KEYS
from app.jobs_v22.digest import canonical_json_bytes, request_digest, verified_request_digest
from app.jobs_v22.errors import DeterministicJobError, TransientJobError
from app.jobs_v22.executor import CollectedProspectSources, ProspectV22Executor
from app.report_v22.capability import BusinessScope
from app.report_v22.capability_public import build_public_capability_report
from app.report_v22.models import TargetMarket
from app.report_v22.public_gbp_models import CustomerPublicGbpReference
from findings_helpers import collection, market, site
from public_gbp_helpers import sample_input, public_source
from test_v22_competitor_collection_stage import request, discovery


@pytest.fixture
def anyio_backend():
    return "asyncio"


def inputs(records=None):
    req = request()
    pages = site(records or [{"status_code": 503}])
    serp = market([("example.test", 1, "provider_position")])
    rivals = collection(serp, types=("home", "home", "home"))
    raw = sample_input()
    public = public_source(raw)
    req.business_identity.site_url = pages.payload.root_url
    req.business_identity.normalized_domain = pages.payload.canonical_host
    req.target_market = TargetMarket(display_name=serp.payload.target_point.requested_label,
        country_code=serp.payload.country_code, latitude=serp.payload.target_point.latitude,
        longitude=serp.payload.target_point.longitude)
    req.business_identity.primary_location = req.target_market
    req.business_identity.public_gbp_url = raw["reference"]["public_gbp_url"]
    req.queries = serp.payload.queries
    req.competitors = [item.competitor for item in rivals.payload.competitors]
    req = AnalyzeRequest.model_validate_json(canonical_json_bytes(req))
    shared = SharedMarketSnapshot(schema_version="competitor_shared_market_v1",
        snapshot_id=serp.binding.snapshot_id, source_job_id=serp.payload.job_id,
        input_digest=analysis_discovery_input_digest(req), snapshot_checksum=request_digest(serp.payload),
        created_at=serp.payload.started_at, expires_at=public.payload.expires_at, snapshot=serp.payload)
    found = discovery(shared)
    job = uuid4()
    rivals.payload.job_id = job
    rivals.payload.discovery_id = found.discovery_id
    rivals.payload.candidate_digest = found.candidate_digest
    rivals.payload.market_snapshot_id = shared.snapshot_id
    rivals.payload.market_snapshot_checksum = shared.snapshot_checksum
    collected = CollectedProspectSources(pages.payload, found, shared, rivals.payload,
        CustomerPublicGbpCollection(reference=CustomerPublicGbpReference.model_validate(raw["reference"]), snapshot=public.payload))
    created = min(pages.payload.started_at, rivals.payload.started_at) - timedelta(seconds=1)
    now = max(pages.payload.completed_at, rivals.payload.completed_at, serp.payload.completed_at,
              public.payload.completed_at) + timedelta(seconds=1)
    user = uuid4()
    business = BusinessScope(id=uuid4(), user_id=user, case_id=req.case_id, revision=1,
        site_url=str(req.business_identity.site_url), primary_service=req.primary_service,
        target_market=req.target_market.display_name, target_pages=[],
        competitor_sites=[str(c.website_url) for c in req.competitors], priority=None,
        confirmation_source="seo_confirmation", confirmed_at=stamp(created))
    envelope = AnalysisRequestEnvelope(schema_version="v22_analysis_request_envelope_v1", analyze_request=req,
        competitor_discovery=AnalysisDiscoveryLink(discovery_id=found.discovery_id,
            candidate_digest=found.candidate_digest,market_snapshot_id=shared.snapshot_id,
            market_snapshot_checksum=shared.snapshot_checksum))
    args = dict(user_id=user, job_id=job, business=business, request=req, collected=collected,
        created_at=created, deadline_at=now+timedelta(minutes=20), now=now)
    receipt = dict(schema_version="v22_acquisition_preparation_v1", user_id=str(user), case_id=str(req.case_id),
        job_id=str(job), idempotency_key="original-reserved-work", business=business.model_dump(mode="json"),
        request_checksum=verified_request_digest(envelope.model_dump(mode="json")),
        created_at=created.isoformat(), deadline_at=args["deadline_at"].isoformat(), run_generation=1, cost_counters={}, dispatch=None)
    return args, envelope, receipt


def dispatch_for(args, manifest):
    return dict(schema_version="v22_capability_dispatch_v1",user_id=str(args["user_id"]),
        case_id=str(manifest.case_id),job_id=str(args["job_id"]),idempotency_key="original-reserved-work",
        run_generation=1,revision=1,attempt_count=0,cost_counters={},status="queued",completion=None,
        created_at=args["created_at"].isoformat(),deadline_at=args["deadline_at"].isoformat(),
        request=dict(schema_version="v22_capability_request_envelope_v1",capability_request=dict(
            case_id=str(manifest.case_id),manifest_id=str(manifest.id),input_checksum=manifest.input_checksum,report_type="prospect")))


@pytest.mark.parametrize("records", [[{"title":"Distinct plumbing"}], [{"status_code":503}],
    [{"status_code":503},{"title":"Same"},{"title":"Same"}]])
def test_collected_source_bridge_runs_actual_rules_and_preserves_original_observations(records):
    args, _, _ = inputs(records)
    before = canonical_json_bytes(args["collected"].site_inventory)
    manifest, value = build_acquisition_input(**args)
    assert canonical_json_bytes(args["collected"].site_inventory) == before
    assert value.evidence_input.sources[0].binding.expires_at == args["deadline_at"]
    assert value.evidence_input.sources[1].binding.expires_at == args["collected"].shared_market.expires_at
    report = build_public_capability_report(manifest, value)
    assert report.id == args["job_id"] and 0 <= len(report.actions) <= 3
    assert report.settlement_policy == "one_credit_per_completed_analysis"
    if records == [{"title":"Distinct plumbing"}]:
        assert report.actions == [] and report.action_state == "insufficient_basis"
    # Retry clock never changes the frozen evaluation time or input digest.
    replay, again = build_acquisition_input(**{**args,"now":args["now"]+timedelta(minutes=1)})
    assert replay == manifest and again == value
    _, _, recipe = generation_recipe(manifest,value)
    assert all("payload" not in s for s in recipe["request"]["evidence_input"]["sources"])


@pytest.mark.parametrize("mutation", ["owner","case","service","competitors","old-site","old-competitor",
    "other-job","wrong-market","expired-market","expired-gbp","future","deadline","wrong-gbp","no-gbp","site-host"])
def test_ineligible_sources_never_receive_a_new_timestamp_or_manifest(mutation):
    args, _, _ = inputs()
    if mutation=="owner": args["user_id"]=uuid4()
    if mutation=="case": args["request"].case_id=uuid4()
    if mutation=="service": args["business"].primary_service="Different service"
    if mutation=="competitors": args["business"].competitor_sites=["https://another.test/"]
    if mutation=="old-site": args["collected"].site_inventory.started_at=args["created_at"]-timedelta(seconds=1)
    if mutation=="old-competitor": args["collected"].competitor_collection.started_at=args["created_at"]-timedelta(seconds=1)
    if mutation=="other-job": args["collected"].competitor_collection.job_id=uuid4()
    if mutation=="wrong-market": args["collected"].competitor_collection.market_snapshot_id=uuid4()
    if mutation=="expired-market": args["collected"].shared_market.expires_at=args["now"]
    if mutation=="expired-gbp": args["collected"].customer_public_gbp.snapshot.expires_at=args["now"]
    if mutation=="future": args["now"]=args["created_at"]
    if mutation=="deadline": args["now"]=args["deadline_at"]
    if mutation=="wrong-gbp": args["collected"].customer_public_gbp.reference.case_id=uuid4()
    if mutation=="no-gbp": args["collected"].customer_public_gbp.snapshot.health_status="unavailable"
    if mutation=="site-host": args["collected"].site_inventory.root_url="https://another.test/"
    with pytest.raises(DeterministicJobError,match="^V22_ACQUISITION_INPUT_INVALID$"):
        build_acquisition_input(**args)


@pytest.mark.anyio
async def test_prepare_uses_real_collector_orchestration_then_atomic_repository_and_small_dispatch():
    args, envelope, receipt = inputs()
    manifest, value = build_acquisition_input(**args)
    dispatch = dispatch_for(args,manifest)
    calls=[]
    def respond(sent):
        import json
        body=json.loads(sent.content); calls.append((sent.url.path,body))
        if sent.url.path.endswith("begin_v22_acquisition_preparation"): return httpx.Response(200,json=receipt)
        if sent.url.path.endswith("prepare_v22_capability_analysis_input"):
            assert body["p_manifest"]==manifest.model_dump(mode="json")
            assert json.loads(body["p_generation_canonical"])==value.model_dump(mode="json")
            return httpx.Response(200,json=dict(manifest_id=str(manifest.id),input_checksum=manifest.input_checksum,
                generation_input_checksum=manifest.generation_input_checksum))
        return httpx.Response(200,json=dispatch)
    source=args["collected"]
    collector=ProspectV22Executor(
        discovery_store=SimpleNamespace(require_state=AsyncMock(return_value=SimpleNamespace(status="succeeded",result=source.discovery))),
        market_store=SimpleNamespace(get=AsyncMock(return_value=source.shared_market)),
        site_stage=SimpleNamespace(collect=AsyncMock(return_value=source.site_inventory)),
        competitor_stage=SimpleNamespace(collect=AsyncMock(return_value=source.competitor_collection)),
        customer_public_gbp_stage=SimpleNamespace(collect=AsyncMock(return_value=source.customer_public_gbp)),
        report_pipeline=SimpleNamespace(build=AsyncMock()),clock=lambda:args["now"])
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        repo=SupabaseCapabilityInputRepository(url="https://store.example",service_role_key="fixture",http_client=client)
        prep=CapabilityAcquisitionPreparer(repository=repo,collector=collector,
            discovery_verifier=SimpleNamespace(verify=AsyncMock(return_value=envelope.competitor_discovery)),clock=lambda:args["now"])
        result=await prep.prepare(user_id=args["user_id"],job_id=args["job_id"],idempotency_key=receipt["idempotency_key"],
            business_version_id=args["business"].id,request=envelope,checkpoints=SimpleNamespace(run_generation=1, report_progress=AsyncMock()),
            cost_ledger=SimpleNamespace(job_id=args["job_id"],job_created_at=args["created_at"]))
    assert result.request.capability_request.manifest_id==manifest.id
    assert len(calls)==3 and all("debit" not in path for path,_ in calls)
    collector.report_pipeline.build.assert_not_called()
    assert collector.site_stage.collect.call_count==1


@pytest.mark.anyio
async def test_committed_prepare_response_loss_recovers_without_recollection_or_second_prepare():
    args,envelope,receipt=inputs(); manifest,_=build_acquisition_input(**args)
    dispatch=dispatch_for(args,manifest); committed=False; prepares=0
    def respond(sent):
        nonlocal committed,prepares
        if sent.url.path.endswith("begin_v22_acquisition_preparation"):
            return httpx.Response(200,json={**receipt,"dispatch":dispatch if committed else None})
        prepares+=1; committed=True
        raise httpx.ReadTimeout("private response lost",request=sent)
    collector=SimpleNamespace(collect_sources=AsyncMock(return_value=args["collected"]))
    verifier=SimpleNamespace(verify=AsyncMock(return_value=envelope.competitor_discovery))
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        repo=SupabaseCapabilityInputRepository(url="https://store.example",service_role_key="fixture",http_client=client)
        prep=CapabilityAcquisitionPreparer(repository=repo,collector=collector,discovery_verifier=verifier,clock=lambda:args["now"])
        call=dict(user_id=args["user_id"],job_id=args["job_id"],idempotency_key=receipt["idempotency_key"],
            business_version_id=args["business"].id,request=envelope,checkpoints=SimpleNamespace(run_generation=1, report_progress=AsyncMock()),
            cost_ledger=SimpleNamespace(job_id=args["job_id"],job_created_at=args["created_at"]))
        with pytest.raises(TransientJobError): await prep.prepare(**call)
        result=await prep.prepare(**call)
    assert result.request.capability_request.manifest_id==manifest.id
    assert prepares==1 and collector.collect_sources.call_count==1 and verifier.verify.call_count==1


@pytest.mark.anyio
@pytest.mark.parametrize("field",["user_id","job_id","case_id","request_checksum","idempotency_key","business"])
async def test_invalid_registration_receipt_blocks_collection(field):
    args,envelope,receipt=inputs()
    changed=deepcopy(receipt)
    changed[field] = {**changed["business"],"primary_service":"Other"} if field=="business" else str(uuid4())
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _:httpx.Response(200,json=changed))) as client:
        repo=SupabaseCapabilityInputRepository(url="https://store.example",service_role_key="fixture",http_client=client)
        with pytest.raises(TransientJobError):
            await begin_acquisition_preparation(repo,user_id=args["user_id"],job_id=args["job_id"],
                idempotency_key=receipt["idempotency_key"],business_version_id=args["business"].id,request=envelope)


@pytest.mark.anyio
@pytest.mark.parametrize("mutation",["deadline","generation","budget","discovery"])
async def test_preparation_cannot_reset_time_generation_or_cost_identity(mutation):
    args,envelope,receipt=inputs()
    collector=SimpleNamespace(collect_sources=AsyncMock())
    verifier=SimpleNamespace(verify=AsyncMock(return_value=envelope.competitor_discovery))
    checkpoints=SimpleNamespace(run_generation=2 if mutation=="generation" else 1)
    ledger=SimpleNamespace(job_id=uuid4() if mutation=="budget" else args["job_id"],job_created_at=args["created_at"])
    if mutation=="discovery": verifier.verify.return_value=envelope.competitor_discovery.model_copy(update={"discovery_id":uuid4()})
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _:httpx.Response(200,json=receipt))) as client:
        repo=SupabaseCapabilityInputRepository(url="https://store.example",service_role_key="fixture",http_client=client)
        prep=CapabilityAcquisitionPreparer(repository=repo,collector=collector,discovery_verifier=verifier,
            clock=lambda:args["deadline_at"] if mutation=="deadline" else args["now"])
        with pytest.raises(DeterministicJobError):
            await prep.prepare(user_id=args["user_id"],job_id=args["job_id"],idempotency_key=receipt["idempotency_key"],
                business_version_id=args["business"].id,request=envelope,checkpoints=checkpoints,cost_ledger=ledger)
    collector.collect_sources.assert_not_called()


@pytest.mark.anyio
async def test_missing_cost_ledger_after_recorded_spend_does_not_start_collection_again():
    args,envelope,receipt=inputs()
    receipt["cost_counters"]={**dict.fromkeys(ALLOWED_COUNTER_KEYS,0),"cost_schema_version":1,"provider_attempts_total":1}
    collector=SimpleNamespace(collect_sources=AsyncMock())
    ledger=SimpleNamespace(job_id=args["job_id"],job_created_at=args["created_at"],
        redis=SimpleNamespace(exists=AsyncMock(return_value=False)),key="original-cost-ledger")
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _:httpx.Response(200,json=receipt))) as client:
        repo=SupabaseCapabilityInputRepository(url="https://store.example",service_role_key="fixture",http_client=client)
        prep=CapabilityAcquisitionPreparer(repository=repo,collector=collector,
            discovery_verifier=SimpleNamespace(verify=AsyncMock()),clock=lambda:args["now"])
        with pytest.raises(TransientJobError,match="^CAPABILITY_RECOVERY_REQUIRED$"):
            await prep.prepare(user_id=args["user_id"],job_id=args["job_id"],idempotency_key=receipt["idempotency_key"],
                business_version_id=args["business"].id,request=envelope,checkpoints=SimpleNamespace(run_generation=1, report_progress=AsyncMock()),cost_ledger=ledger)
    collector.collect_sources.assert_not_called()
    ledger.redis.exists.assert_awaited_once_with(ledger.key)
