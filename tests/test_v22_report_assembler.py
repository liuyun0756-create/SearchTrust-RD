from __future__ import annotations

from datetime import date
from uuid import UUID

from app.api.v2.models import AnalyzeRequest
from app.competitors_v22.models import SharedMarketSnapshot
from app.jobs_v22.digest import request_digest
from app.report_v22.action_models import PublicActionPlanInput
from app.report_v22.actions import build_public_action_plan
from app.report_v22.assembler import assemble_prospect_report
from app.report_v22.copy_contract import (
    build_copy_request,
    validate_and_render_copy,
)
from app.report_v22.findings import build_public_findings
from findings_helpers import collection, market, request as findings_request, site
from test_v22_competitor_models import NOW
from v22_copy_helpers import valid_response


REPORT_ID = UUID("55555555-5555-4555-8555-555555555555")


def test_assembler_builds_strict_traceable_prospect_report() -> None:
    site_source = site(
        [
            {"status_code": 503, "title": "Broken"},
            {"status_code": 200, "title": "Shared", "meta_robots": ["noindex"]},
            {"status_code": 200, "title": "Shared"},
        ]
    )
    market_source = market()
    competitor_source = collection(market_source)
    findings = build_public_findings(
        findings_request(site_source, market_source, competitor_source)
    )
    action_plan = build_public_action_plan(
        PublicActionPlanInput(findings_result=findings, planning_date=date(2026, 9, 4))
    )
    copy_request = build_copy_request(findings, action_plan)
    action_copy = validate_and_render_copy(copy_request, valid_response(copy_request))
    context = findings_request(site_source, market_source, competitor_source).evidence_input.context
    analyze = AnalyzeRequest(
        case_id=context.case_id,
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
        expires_at=NOW.replace(day=31),
        snapshot=market_source.payload,
    )

    report = assemble_prospect_report(
        report_id=REPORT_ID,
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

    assert report.report_version.report_id == REPORT_ID
    assert report.report_version.report_type == "prospect"
    assert len(report.top_actions) == 3
    assert len(report.competitor_analysis.competitors) == 3
    assert report.client_summary.action_ids == [item.action_id for item in report.top_actions]
    assert report.data_coverage.full_evidence_coverage is False
    assert report.first_party_performance.gsc.connection_state == "not_connected"
    assert report.first_party_performance.gbp.connection_state == "not_connected"
    assert report.first_party_performance.ga4.connection_state == "not_connected"
    assert all(
        item.evidence_ids for item in report.competitor_analysis.competitors
    )
