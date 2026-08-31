import pytest

from app.jobs_v22.digest import canonical_json_bytes
from app.report_v22.findings import build_public_findings
from app.report_v22.findings_errors import FindingsError
from app.report_v22.findings_models import PublicFindingsInput
from app.report_v22.public_rule_catalog import GBP_RULES
from evidence_helpers import NOW, site_source
from public_gbp_helpers import evidence_input, sample_input


@pytest.mark.parametrize("scenario,expected", [("matched", "gbp_alignment_not_implemented"), ("expired", "source_ineligible"), ("identity_conflict", "source_ineligible"), ("partial", "field_not_observed"), ("missing", "customer_public_gbp_missing")])
def test_gbp_checks_report_coverage_only(scenario, expected):
    raw = sample_input()
    if scenario == "expired":
        raw["expires_at"] = NOW
    elif scenario == "identity_conflict":
        raw["record"]["observed_entity_keys"][0]["value"] = "conflicting"
    elif scenario == "partial":
        for field in ("business_name", "address", "phone", "service_areas"):
            raw["record"]["fields"][field] = dict(state="not_returned", value=[] if field == "service_areas" else None)
    request = evidence_input(raw)
    if scenario == "missing":
        request.sources = []
    result = build_public_findings(PublicFindingsInput(evidence_input=request))
    evaluations = [e for e in result.rule_evaluations if e.rule_id in GBP_RULES]
    assert len(evaluations) == 4 and result.findings == []
    for evaluation in evaluations:
        assert evaluation.state == "not_checked" and evaluation.reason == expected
        assert evaluation.comparator_ids == [] and evaluation.finding_id is None
        assert bool(evaluation.evidence_ids) == (scenario != "missing")
        assert bool(evaluation.target.snapshot_id) == (scenario != "missing")
    assert all(layer.status == "not_checked" and not layer.evidence_ids and not layer.finding_ids for layer in result.site_rollup.layers)
    if scenario == "identity_conflict":
        assert all(any("strong_id_conflict" in note for note in e.limitations) for e in evaluations)
    elif scenario == "expired":
        assert all(any("expired" in note for note in e.limitations) for e in evaluations)


@pytest.mark.parametrize("tamper", ["field", "site", "comparator", "reason", "snapshot", "omit", "target"])
def test_gbp_reference_audit_rejects_wrong_branch_and_field(monkeypatch, tamper):
    from app.report_v22 import public_gbp_findings
    evaluate = public_gbp_findings.evaluate

    def forged(view):
        outcomes = list(evaluate(view))
        first, second = outcomes[0].evaluation, outcomes[1].evaluation
        if tamper == "field":
            first.evidence_ids = second.evidence_ids
        elif tamper == "site":
            first.evidence_ids = [next(k for k, item in view.items.items() if item.source_type == "site")]
        elif tamper == "comparator":
            first.comparator_ids = second.evidence_ids
        elif tamper == "reason":
            first.reason = "customer_public_gbp_missing"
        elif tamper == "snapshot":
            first.target.snapshot_id = None
        elif tamper == "target":
            first.target.kind = "site"
        else:
            first.evidence_ids = []
        return outcomes

    monkeypatch.setattr(public_gbp_findings, "evaluate", forged)
    with pytest.raises(FindingsError, match="V22_FINDINGS_REFERENCE_INVALID"):
        build_public_findings(PublicFindingsInput(evidence_input=evidence_input(sample_input(), site_source())))


def test_existing_rules_and_ids_unchanged_when_public_profile_added():
    from findings_helpers import site, market, collection
    serp = market()
    request = evidence_input(sample_input(), site([{"status_code": 404}, {"meta_robots": ["noindex"]}]), serp, collection(serp))
    with_gbp = build_public_findings(PublicFindingsInput(evidence_input=request))
    request.sources = request.sources[1:]
    request.context.customer_public_gbp = None
    without = build_public_findings(PublicFindingsInput(evidence_input=request))
    assert len(without.findings) >= 2
    assert canonical_json_bytes([f.model_dump(mode="json") for f in with_gbp.findings]) == canonical_json_bytes([f.model_dump(mode="json") for f in without.findings])
    assert [e for e in with_gbp.rule_evaluations if e.rule_id not in GBP_RULES] == [e for e in without.rule_evaluations if e.rule_id not in GBP_RULES]
