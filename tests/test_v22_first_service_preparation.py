from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

from app.jobs_v22.capability_acquisition_input import _build_collected_input
from app.jobs_v22.capability_first_service_preparation import CapabilityFirstServicePreparer, begin_first_service_preparation
from app.jobs_v22.capability_input_repository import SupabaseCapabilityInputRepository
from app.jobs_v22.digest import canonical_json_bytes, verified_request_digest
from app.jobs_v22.errors import DeterministicJobError, TransientJobError
from app.jobs_v22.executor import ProspectV22Executor
from app.jobs_v22.first_service_collection_models import FirstServiceCollectionEnvelope
from app.report_v22.capability_service import build_service_capability_report
from test_v22_capability_acquisition_preparation import inputs as public_inputs, dispatch_for as public_dispatch
from test_v22_capability_service import inputs as service_inputs


@pytest.fixture
def anyio_backend():
    return "asyncio"


def inputs():
    args, old, receipt = public_inputs()
    req = old.analyze_request.model_dump(mode="python", exclude={"report_type", "first_party_snapshots", "parent_report"})
    envelope = FirstServiceCollectionEnvelope(schema_version="v22_first_service_collection_v1",
        collection_request=req, competitor_discovery=old.competitor_discovery)
    _, service, _ = service_inputs()
    for s in service.first_party_input.snapshots:
        s.case_id = args["business"].case_id
        s.fetched_at = args["created_at"] - timedelta(seconds=1)
        s.expires_at = args["deadline_at"] + timedelta(hours=1)
    args["request"] = envelope.collection_request
    args["private_snapshots"] = service.first_party_input.snapshots
    receipt.update(schema_version="v22_first_service_collection_preparation_v1",
        request_checksum=verified_request_digest(envelope.model_dump(mode="json")),
        snapshots=[s.model_dump(mode="json") for s in args["private_snapshots"]])
    return args, envelope, receipt


def dispatch_for(args, manifest):
    result = public_dispatch(args, manifest)
    result["request"]["capability_request"]["report_type"] = "verified_execution"
    return result


def test_actual_source_adapter_and_service_rules_create_first_report_without_parent():
    args, envelope, _ = inputs()
    before = [s.model_dump(mode="json") for s in args["private_snapshots"]]
    manifest, value = _build_collected_input(**args)
    report = build_service_capability_report(manifest, value)
    assert report.report_type == "verified_execution" and report.version == 1
    assert report.input_manifest.mode == "first_service" and report.input_manifest.previous_report is None
    assert report.changes == [] and len(manifest.snapshots) == 6
    assert {"gsc", "ga4", "site"} <= {e.source_kind for e in report.evidence}
    assert [s.model_dump(mode="json") for s in args["private_snapshots"]] == before
    assert "report_type" not in envelope.collection_request.model_dump()
    assert _build_collected_input(**{**args, "now":args["now"]+timedelta(seconds=1)}) == (manifest, value)


@pytest.mark.parametrize("mutation", ["foreign-case", "expired", "unhealthy", "mismatch", "future", "missing", "duplicate", "owner", "deadline"])
def test_invalid_private_sources_or_task_never_become_a_manifest(mutation):
    args, _, _ = inputs()
    s = args["private_snapshots"][0]
    if mutation == "foreign-case": s.case_id = uuid4()
    if mutation == "expired": s.expires_at = args["now"]
    if mutation == "unhealthy": s.health_status = "unhealthy"
    if mutation == "mismatch": s.identity_match_status = "mismatch"
    if mutation == "future": s.fetched_at = args["deadline_at"]
    if mutation == "missing": args["private_snapshots"].pop()
    if mutation == "duplicate": args["private_snapshots"][1] = s
    if mutation == "owner": args["user_id"] = uuid4()
    if mutation == "deadline": args["now"] = args["deadline_at"]
    with pytest.raises(DeterministicJobError, match="V22_FIRST_SERVICE_INPUT_INVALID"):
        _build_collected_input(**args)


@pytest.mark.parametrize("field", ["report_type", "parent_report", "first_party_snapshots"])
def test_collection_intent_cannot_smuggle_a_legacy_report_or_private_input(field):
    _, envelope, _ = inputs()
    raw = envelope.model_dump(mode="json")
    raw["collection_request"][field] = None
    with pytest.raises(ValueError): FirstServiceCollectionEnvelope.model_validate(raw)


@pytest.mark.anyio
async def test_real_source_only_orchestration_and_repository_do_not_execute_or_charge_prospect():
    args, envelope, receipt = inputs()
    manifest, value = _build_collected_input(**args)
    source = args["collected"]
    collector = ProspectV22Executor(
        discovery_store=SimpleNamespace(require_state=AsyncMock(return_value=SimpleNamespace(status="succeeded", result=source.discovery))),
        market_store=SimpleNamespace(get=AsyncMock(return_value=source.shared_market)),
        site_stage=SimpleNamespace(collect=AsyncMock(return_value=source.site_inventory)),
        competitor_stage=SimpleNamespace(collect=AsyncMock(return_value=source.competitor_collection)),
        customer_public_gbp_stage=SimpleNamespace(collect=AsyncMock(return_value=source.customer_public_gbp)),
        report_pipeline=SimpleNamespace(build=AsyncMock()), clock=lambda:args["now"])
    calls = []
    def respond(sent):
        import json
        body=json.loads(sent.content); calls.append(sent.url.path)
        if sent.url.path.endswith("begin_v22_first_service_collection"): return httpx.Response(200,json=receipt)
        if sent.url.path.endswith("prepare_v22_capability_analysis_input"):
            assert body["p_manifest"] == manifest.model_dump(mode="json")
            assert json.loads(body["p_generation_canonical"]) == value.model_dump(mode="json")
            return httpx.Response(200,json=dict(manifest_id=str(manifest.id),input_checksum=manifest.input_checksum,
                generation_input_checksum=manifest.generation_input_checksum))
        return httpx.Response(200,json=dispatch_for(args,manifest))
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        repo=SupabaseCapabilityInputRepository(url="https://storage.example",service_role_key="fixture",http_client=client)
        preparer=CapabilityFirstServicePreparer(repository=repo,collector=collector,
            discovery_verifier=SimpleNamespace(verify=AsyncMock(return_value=envelope.competitor_discovery)),clock=lambda:args["now"])
        await preparer.prepare(user_id=args["user_id"],job_id=args["job_id"],idempotency_key=receipt["idempotency_key"],
            business_version_id=args["business"].id,request=envelope,checkpoints=SimpleNamespace(run_generation=1, report_progress=AsyncMock()),
            cost_ledger=SimpleNamespace(job_id=args["job_id"],job_created_at=args["created_at"]))
    collector.report_pipeline.build.assert_not_called()
    assert collector.site_stage.collect.call_count == 1
    assert collector.site_stage.collect.call_args.kwargs["request"] == envelope.collection_request
    assert len(calls) == 3 and not any("debit" in c or "start_" in c for c in calls)


@pytest.mark.anyio
@pytest.mark.parametrize("field", ["user_id", "job_id", "case_id", "request_checksum", "idempotency_key", "snapshots"])
async def test_invalid_receipts_are_rejected_before_collection(field):
    args, envelope, receipt = inputs()
    receipt[field] = [] if field == "snapshots" else str(uuid4())
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _:httpx.Response(200,json=receipt))) as client:
        repo=SupabaseCapabilityInputRepository(url="https://storage.example",service_role_key="fixture",http_client=client)
        with pytest.raises(TransientJobError):
            await begin_first_service_preparation(repo,user_id=args["user_id"],job_id=args["job_id"],
                idempotency_key="original-reserved-work",business_version_id=args["business"].id,request=envelope)


@pytest.mark.anyio
async def test_actual_site_stage_accepts_source_only_request_and_recovers_checkpoint():
    import fakeredis.aioredis
    from app.jobs_v22.checkpoints import JobCheckpoints
    from app.jobs_v22.site_inventory_stage import CheckpointedSiteInventoryStage, SiteInventoryStageConfig
    from app.jobs_v22.first_service_collection_models import FirstServiceCollectionRequest
    from test_v22_site_inventory_stage import prospect_request, RecordingFetcher, stage as make_stage
    old = prospect_request()
    request = FirstServiceCollectionRequest.model_validate_json(canonical_json_bytes(old.model_dump(mode="json",
        exclude={"report_type", "first_party_snapshots", "parent_report"})))
    redis = fakeredis.aioredis.FakeRedis()
    fetcher = RecordingFetcher()
    stage = make_stage(fetcher, None)
    checkpoints = JobCheckpoints(redis, prefix="test:first-service:site", ttl_seconds=3600)
    job = uuid4()
    try:
        first = await stage.collect(job_id=job, request=request, checkpoints=checkpoints)
        calls = list(fetcher.calls)
        again = await stage.collect(job_id=job, request=request, checkpoints=checkpoints)
        assert first == again and fetcher.calls == calls and calls
    finally:
        await redis.aclose()


@pytest.mark.anyio
@pytest.mark.parametrize("mutation", ["generation", "budget", "deadline", "discovery"])
async def test_first_service_preparation_keeps_original_execution_identity(mutation):
    args, envelope, receipt = inputs()
    collector=SimpleNamespace(collect_confirmed_sources=AsyncMock())
    verifier=SimpleNamespace(verify=AsyncMock(return_value=envelope.competitor_discovery))
    if mutation == "discovery": verifier.verify.return_value=envelope.competitor_discovery.model_copy(update={"discovery_id":uuid4()})
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _:httpx.Response(200,json=receipt))) as client:
        repo=SupabaseCapabilityInputRepository(url="https://storage.example",service_role_key="fixture",http_client=client)
        preparer=CapabilityFirstServicePreparer(repository=repo,collector=collector,discovery_verifier=verifier,
            clock=lambda:args["deadline_at"] if mutation == "deadline" else args["now"])
        with pytest.raises(DeterministicJobError):
            await preparer.prepare(user_id=args["user_id"],job_id=args["job_id"],idempotency_key=receipt["idempotency_key"],
                business_version_id=args["business"].id,request=envelope,
                checkpoints=SimpleNamespace(run_generation=2 if mutation == "generation" else 1),
                cost_ledger=SimpleNamespace(job_id=uuid4() if mutation == "budget" else args["job_id"],job_created_at=args["created_at"]))
    collector.collect_confirmed_sources.assert_not_called()
