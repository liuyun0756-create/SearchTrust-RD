from datetime import date, timedelta

from app.jobs_v22.digest import request_digest
from app.report_v22.action_models import PublicActionPlanInput
from app.report_v22.actions import build_public_action_plan, canonical_public_findings
from app.report_v22.cross_source_findings import build_cross_source_findings
from app.report_v22.cross_source_findings_models import CrossSourceFindingsInput
from app.report_v22.findings import build_public_findings
from app.report_v22.first_party_findings import build_first_party_findings
from app.report_v22.first_party_findings_models import FirstPartyFindingsInput
from app.report_v22.verified_reprioritization_models import VerifiedReprioritizationInput
from evidence_helpers import CASE
from findings_helpers import collection, market, request as public_input, site
from test_v22_first_party_findings import NOW, PARENT_ID, ga4_value, gsc_value, trusted


VERIFIED_AT = NOW + timedelta(hours=1)
PLANNING_DATE = date(2026, 9, 8)


def verified_request(*, gsc=None, ga4=None):
    serp = market()
    public_request = public_input(
        site([
            {"status_code": 503, "title": "Broken"},
            {"status_code": 200, "title": "Shared", "meta_robots": ["noindex"]},
            {"status_code": 200, "title": "Shared"},
        ]),
        serp,
        collection(serp),
    )
    public_result = build_public_findings(public_request)
    public_plan = build_public_action_plan(PublicActionPlanInput(
        findings_result=public_result, planning_date=date(2026, 9, 2),
    ))
    snapshots = [
        trusted("gsc", gsc or gsc_value(), 950).model_copy(update={"case_id": CASE}),
        trusted("ga4", ga4 or ga4_value(), 951).model_copy(update={"case_id": CASE}),
    ]
    first_input = FirstPartyFindingsInput(
        case_id=CASE,
        parent_report_id=PARENT_ID,
        evaluated_at=VERIFIED_AT,
        snapshots=snapshots,
    )
    first_result = build_first_party_findings(first_input)
    first_input_checksum = request_digest(first_input)
    first_result_checksum = request_digest(first_result)
    cross_input = CrossSourceFindingsInput(
        case_id=CASE,
        parent_report_id=PARENT_ID,
        evaluated_at=VERIFIED_AT,
        normalized_domain="example.test",
        first_party_input=first_input,
        first_party_result=first_result,
        first_party_input_checksum=first_input_checksum,
        first_party_result_checksum=first_result_checksum,
    )
    cross_result = build_cross_source_findings(cross_input)
    return VerifiedReprioritizationInput(
        case_id=CASE,
        parent_report_id=PARENT_ID,
        evaluated_at=VERIFIED_AT,
        planning_date=PLANNING_DATE,
        public_findings_input=public_request,
        public_findings_result=public_result,
        public_findings_input_checksum=request_digest(public_request),
        public_findings_result_checksum=request_digest(canonical_public_findings(public_result)),
        public_action_plan=public_plan,
        public_action_plan_checksum=request_digest(public_plan),
        first_party_input=first_input,
        first_party_result=first_result,
        first_party_input_checksum=first_input_checksum,
        first_party_result_checksum=first_result_checksum,
        cross_source_normalized_domain="example.test",
        cross_source_result=cross_result,
        cross_source_result_checksum=request_digest(cross_result),
    )
