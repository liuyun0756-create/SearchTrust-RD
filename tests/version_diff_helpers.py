from datetime import date, timedelta

from app.api.v2.models import AnalyzeRequest
from app.competitors_v22.models import SharedMarketSnapshot
from app.jobs_v22.digest import request_digest
from app.report_v22.action_models import PublicActionPlanInput
from app.report_v22.actions import build_public_action_plan
from app.report_v22.assembler import assemble_prospect_report
from app.report_v22.copy_contract import build_copy_request, validate_and_render_copy
from app.report_v22.findings import build_public_findings
from app.report_v22.version_diff_models import VersionDiffBuildInput
from app.report_v22.verified_reprioritization import build_verified_reprioritization
from evidence_helpers import CASE
from findings_helpers import collection, market, request as public_input, site
from test_v22_first_party_findings import NOW, PARENT_ID
from v22_copy_helpers import valid_response
from verified_reprioritization_helpers import verified_request


def parent_report():
    site_source = site([
        {"status_code": 503, "title": "Broken"},
        {"status_code": 200, "title": "Shared", "meta_robots": ["noindex"]},
        {"status_code": 200, "title": "Shared"},
    ])
    market_source = market()
    competitor_source = collection(market_source)
    public_request = public_input(site_source, market_source, competitor_source)
    findings = build_public_findings(public_request)
    action_plan = build_public_action_plan(PublicActionPlanInput(
        findings_result=findings,
        planning_date=date(2026, 9, 2),
    ))
    copy_request = build_copy_request(findings, action_plan)
    action_copy = validate_and_render_copy(copy_request, valid_response(copy_request))
    context = public_request.evidence_input.context
    analyze = AnalyzeRequest(
        case_id=CASE,
        report_type="prospect",
        business_identity={
            "business_name": "Example Plumbing",
            "site_url": context.site_url,
            "normalized_domain": "example.test",
            "operating_model": "service_area",
            "primary_location": context.target_market,
            "public_gbp_url": None,
        },
        primary_service=context.primary_service,
        target_market=context.target_market,
        queries=context.queries,
        competitors=context.competitors,
    )
    shared = SharedMarketSnapshot(
        schema_version="competitor_shared_market_v1",
        snapshot_id=market_source.binding.snapshot_id,
        source_job_id=market_source.payload.job_id,
        input_digest="sha256:" + "a" * 64,
        snapshot_checksum=request_digest(market_source.payload),
        created_at=NOW,
        expires_at=NOW + timedelta(days=1),
        snapshot=market_source.payload,
    )
    return assemble_prospect_report(
        report_id=PARENT_ID,
        request=analyze,
        site_inventory=site_source.payload,
        shared_market=shared,
        competitor_collection=competitor_source.payload,
        findings=findings,
        action_plan=action_plan,
        action_copy=action_copy,
        generated_at=NOW,
        copy_model_version="dify-controlled-copy-v1",
    )


def version_diff_request(**verified_kwargs):
    upstream = verified_request(**verified_kwargs)
    verified = build_verified_reprioritization(upstream)
    parent = parent_report()
    return VersionDiffBuildInput(
        case_id=CASE,
        parent_report_id=PARENT_ID,
        evaluated_at=upstream.evaluated_at,
        parent_report=parent,
        parent_report_checksum=request_digest(parent),
        verified_reprioritization_input=upstream,
        verified_reprioritization_input_checksum=request_digest(upstream),
        verified_reprioritization_result=verified,
        verified_reprioritization_result_checksum=request_digest(verified),
    )
