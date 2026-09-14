"""Real deterministic public stages and frozen snapshots for pipeline tests."""

from datetime import timedelta
from uuid import UUID

from app.api.v2.models import AnalyzeRequest
from app.competitors_v22.models import SharedMarketSnapshot
from app.competitors_v22.selection import analysis_discovery_input_digest
from app.jobs_v22.digest import request_digest, verified_request_digest
from app.report_v22.action_models import PublicActionPlanInput
from app.report_v22.actions import build_public_action_plan
from app.report_v22.assembler import assemble_prospect_report
from app.report_v22.copy_contract import build_copy_request, validate_and_render_copy
from app.report_v22.findings import build_public_findings
from app.report_v22.findings_models import PublicFindingsInput
from app.report_v22.public_gbp_models import CustomerPublicGbpReference
from evidence_helpers import CASE, NOW, context
from findings_helpers import collection, market, site
from public_gbp_helpers import public_source, sample_input
from test_v22_first_party_findings import NOW as FIRST_PARTY_AT, ga4_value, gsc_value, trusted
from v22_copy_helpers import valid_response


JOB_ID = UUID("55555555-5555-4555-8555-555555555555")
PARENT_ID = UUID("30000000-0000-4000-8000-000000000002")
VERIFIED_AT = FIRST_PARTY_AT + timedelta(hours=1)


def frozen_public_fixture():
    """Assemble the parent from actual source evidence, including public GBP."""
    site_source = site([
        {"status_code": 503, "title": "Broken"},
        {"status_code": 200, "title": "Shared", "meta_robots": ["noindex"]},
        {"status_code": 200, "title": "Shared"},
    ])
    market_source = market()
    competitor_source = collection(market_source)
    competitor_source.payload.job_id = PARENT_ID
    competitor_source.binding.payload_checksum = request_digest(competitor_source.payload)
    public_raw = sample_input()
    gbp_source = public_source(public_raw)
    ctx = context(customer_public_gbp=public_raw["reference"])
    analyze = AnalyzeRequest(
        case_id=CASE, report_type="prospect",
        business_identity=dict(business_name="Example Plumbing", site_url=ctx.site_url,
            normalized_domain="example.test", operating_model="service_area",
            primary_location=ctx.target_market, public_gbp_url=public_raw["reference"]["public_gbp_url"]),
        primary_service=ctx.primary_service, target_market=ctx.target_market,
        queries=ctx.queries, competitors=ctx.competitors,
    )
    shared = SharedMarketSnapshot(schema_version="competitor_shared_market_v1",
        snapshot_id=market_source.binding.snapshot_id, source_job_id=market_source.payload.job_id,
        input_digest=analysis_discovery_input_digest(analyze), snapshot_checksum=request_digest(market_source.payload),
        created_at=NOW, expires_at=NOW + timedelta(days=1), snapshot=market_source.payload)
    market_source.shared_snapshot = shared
    market_source.binding.expires_at = shared.expires_at
    for source in (site_source, market_source, competitor_source):
        source.binding.health_status = "healthy"
        source.binding.identity_match_status = "matched"
    public_input = PublicFindingsInput(evidence_input=dict(context=ctx,
        sources=[site_source, market_source, competitor_source, gbp_source]), business_identity=analyze.business_identity)
    public_result = build_public_findings(public_input)
    public_plan = build_public_action_plan(PublicActionPlanInput(findings_result=public_result, planning_date=NOW.date()))
    copy_request = build_copy_request(public_result, public_plan)
    parent = assemble_prospect_report(report_id=PARENT_ID, request=analyze, site_inventory=site_source.payload,
        shared_market=shared, competitor_collection=competitor_source.payload, findings=public_result,
        action_plan=public_plan, action_copy=validate_and_render_copy(copy_request, valid_response(copy_request)),
        generated_at=NOW, copy_model_version="fixture-copy-v1")
    rows = {}
    for source in (site_source, market_source, competitor_source):
        rows[source.binding.source_type + "_snapshot"] = dict(
            snapshot_id=str(source.binding.snapshot_id), case_id=str(CASE), source_type=source.binding.source_type,
            schema_version=source.payload.schema_version, normalized_payload=source.payload.model_dump(mode="json"),
            payload_checksum=request_digest(source.payload), created_at=NOW.isoformat(),
            fetched_at=source.binding.fetched_at.isoformat(),
            expires_at=source.binding.expires_at.isoformat() if source.binding.expires_at else None)
    rows["public_gbp_snapshot"] = dict(
        snapshot_id=str(gbp_source.binding.snapshot_id), case_id=str(CASE), source_type="gbp",
        schema_version=gbp_source.payload.schema_version,
        normalized_payload=gbp_source.payload.model_dump(mode="json"),
        payload_checksum=request_digest(gbp_source.payload), created_at=gbp_source.binding.fetched_at.isoformat(),
        fetched_at=gbp_source.binding.fetched_at.isoformat(), expires_at=gbp_source.binding.expires_at.isoformat(),
        reference=CustomerPublicGbpReference.model_validate(public_raw["reference"]).model_dump(mode="json"),
    )
    snapshots = [trusted("gsc", gsc_value(), 950), trusted("ga4", ga4_value(), 951)]
    for snapshot in snapshots:
        snapshot.case_id = CASE
    payload = dict(schema_version="v22_verified_resolved_input_v1", job_id=str(JOB_ID), case_id=str(CASE),
        parent_report=parent.model_dump(mode="json"), parent_payload_checksum=verified_request_digest(parent.model_dump(mode="json")),
        first_party_snapshots=[s.model_dump(mode="json") for s in snapshots], **rows)
    return payload, public_input, public_result, public_plan


async def resolve_fixture():
    import httpx

    from app.jobs_v22.verified_input_resolver import SupabaseVerifiedInputResolver
    from app.jobs_v22.verified_models import VerifiedTaskRequest

    payload, _, _, _ = frozen_public_fixture()
    public_id = payload["public_gbp_snapshot"]["snapshot_id"]
    sources = {item["source_type"]: item["snapshot_id"] for item in payload["first_party_snapshots"]}
    identity = dict(case_id=payload["case_id"], job_id=str(JOB_ID), parent_report_id=str(PARENT_ID),
        gsc_snapshot_id=sources["gsc"], ga4_snapshot_id=sources["ga4"], public_gbp_snapshot_id=public_id)
    request = VerifiedTaskRequest.model_validate_json(__import__("json").dumps(
        {**{key: value for key, value in identity.items() if key != "job_id"},
         "input_checksum": verified_request_digest(identity)}))
    encoded = __import__("json").dumps(payload).encode()
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _: httpx.Response(200, stream=httpx.ByteStream(encoded)))) as client:
        resolved = await SupabaseVerifiedInputResolver(url="https://storage.example", service_role_key="secret",
            http_client=client).resolve(job_id=JOB_ID, request=request, run_generation=1)
    return resolved, request
