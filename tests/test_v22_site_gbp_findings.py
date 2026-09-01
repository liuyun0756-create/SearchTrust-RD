from app.report_v22.findings import build_public_findings
from app.report_v22.findings_models import PublicFindingsInput
from app.report_v22.public_rule_catalog import GBP_RULES
from public_gbp_helpers import evidence_input
from site_business_helpers import request
from test_v22_site_gbp_alignment import exact_gbp, exact_html, identity, missing


def build(*, html=None, raw=None, operating_model="hybrid"):
    raw = raw or exact_gbp()
    site_request = request(html if html is not None else exact_html())
    evidence = evidence_input(raw, site_request.source)
    return build_public_findings(PublicFindingsInput(
        evidence_input=evidence,
        business_identity=identity(evidence, operating_model=operating_model),
    ))


def gbp_evaluations(result):
    return {item.rule_id: item for item in result.rule_evaluations if item.rule_id in GBP_RULES}


def test_opt_in_alignment_replaces_coverage_bookkeeping_with_v110_evaluations():
    result = build()
    evaluations = gbp_evaluations(result)
    assert len(evaluations) == 4
    assert {item.rule_version for item in evaluations.values()} == {"1.1.0"}
    assert {item.state for item in evaluations.values()} == {"not_triggered"}
    assert {item.reason for item in evaluations.values()} == {"exact_match"}
    assert result.findings == []


def test_name_mismatch_creates_audited_customer_gbp_finding():
    raw = exact_gbp()
    raw["record"]["fields"]["business_name"]["value"] = "Different Business"
    result = build(raw=raw)
    evaluation = gbp_evaluations(result)["v22_public.gbp_name_alignment"]
    assert evaluation.state == "triggered" and evaluation.reason == "value_mismatch"
    finding = next(item for item in result.findings if item.finding_id == evaluation.finding_id)
    assert finding.rule_version == "1.1.0" and finding.severity == "medium"
    items = {item.evidence_id: item for item in result.evidence_result.evidence_index}
    assert {items[key].source_type for key in evaluation.evidence_ids} == {"site"}
    assert {items[key].source_type for key in evaluation.comparator_ids} == {"gbp"}
    assert finding.finding_id in result.site_rollup.finding_ids


def test_service_area_partial_match_creates_low_severity_improvement_finding():
    raw = exact_gbp()
    raw["record"]["fields"]["service_areas"]["value"] = ["Austin", "Pflugerville"]
    result = build(raw=raw)
    evaluation = gbp_evaluations(result)["v22_public.gbp_service_area_alignment"]
    finding = next(item for item in result.findings if item.finding_id == evaluation.finding_id)
    assert evaluation.reason == "partial_match" and finding.severity == "low"


def test_single_and_double_missing_findings_use_only_dedicated_side_coverage():
    raw = missing(exact_gbp(), "phone")
    result = build(raw=raw)
    evaluation = gbp_evaluations(result)["v22_public.gbp_phone_alignment"]
    assert evaluation.reason == "gbp_field_missing"
    items = {item.evidence_id: item for item in result.evidence_result.evidence_index}
    assert {items[key].source_type for key in evaluation.evidence_ids} == {"site"}
    assert {items[key].source_type for key in evaluation.comparator_ids} == {"coverage"}

    both = build(html=exact_html(telephone=None), raw=raw)
    evaluation = gbp_evaluations(both)["v22_public.gbp_phone_alignment"]
    items = {item.evidence_id: item for item in both.evidence_result.evidence_index}
    assert evaluation.reason == "both_fields_missing"
    assert {items[key].original_value for key in evaluation.evidence_ids} == {"site_missing"}
    assert {items[key].original_value for key in evaluation.comparator_ids} == {"gbp_missing"}


def test_operating_model_not_applicable_never_creates_finding():
    result = build(operating_model="storefront")
    evaluation = gbp_evaluations(result)["v22_public.gbp_service_area_alignment"]
    assert evaluation.state == "not_triggered" and evaluation.reason == "field_not_applicable"
    assert evaluation.finding_id is None
