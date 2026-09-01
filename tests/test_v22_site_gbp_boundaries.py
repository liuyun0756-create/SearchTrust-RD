from datetime import timedelta
import json

import pytest

from app.jobs_v22.digest import request_digest
from app.report_v22 import findings as builder
from app.report_v22.findings_errors import FindingsError
from app.report_v22.findings_models import PublicFindingsInput
from app.report_v22.public_rule_catalog import GBP_RULES
from public_gbp_helpers import evidence_input
from site_business_helpers import request
from test_v22_site_gbp_alignment import exact_gbp, exact_html, identity


def findings_input(*, site=True, public=True, html=None, limits=None):
    raw = exact_gbp()
    site_request = request(html if html is not None else exact_html())
    evidence = evidence_input(raw, *( [site_request.source] if site else [] ))
    if not public:
        evidence.sources = [source for source in evidence.sources if source.kind != "public_gbp"]
    values = dict(evidence_input=evidence, business_identity=identity(evidence))
    if limits is not None:
        values["site_gbp_alignment_limits"] = limits
    return PublicFindingsInput(**values), site_request


@pytest.mark.parametrize("site,public", [(False, True), (True, False), (False, False)])
def test_missing_source_is_not_converted_to_missing_business_fields(site, public):
    value, _ = findings_input(site=site, public=public)
    result = builder.build_public_findings(value)
    evaluations = [item for item in result.rule_evaluations if item.rule_id in GBP_RULES]
    assert len(evaluations) == 4
    assert all(item.state == "not_checked" and item.reason == "source_missing" for item in evaluations)
    assert not any(item.rule_id in GBP_RULES for item in result.findings)


def test_confirmed_identity_mismatch_fails_with_fixed_binding_error():
    value, _ = findings_input()
    changed = value.business_identity.primary_location.model_copy(update={"display_name": "Dallas, TX"})
    value.business_identity = value.business_identity.model_copy(update={"primary_location": changed})
    with pytest.raises(FindingsError, match="V22_FINDINGS_BINDING_INVALID"):
        builder.build_public_findings(value)


def test_opt_in_source_checksum_failure_is_mapped_to_fixed_findings_error():
    value, _ = findings_input()
    site = next(source for source in value.evidence_input.sources if source.kind == "site")
    site.payload.selected_pages[0].deep_snapshot.html += "tampered"
    with pytest.raises(FindingsError, match="V22_FINDINGS_CHECKSUM_MISMATCH"):
        builder.build_public_findings(value)


def test_pair_budget_is_checked_before_cartesian_comparison():
    records = [
        {"@context": "https://schema.org", "@type": "LocalBusiness", "name": "Fixture Plumbing"},
        {"@context": "https://schema.org", "@type": "LocalBusiness", "name": "Other Name"},
    ]
    html = '<script type="application/ld+json">' + json.dumps(records) + "</script>"
    value, _ = findings_input(html=html, limits={"max_pair_comparisons": 1})
    with pytest.raises(FindingsError, match="V22_FINDINGS_LIMIT_EXCEEDED"):
        builder.build_public_findings(value)


def test_snapshots_more_than_thirty_days_apart_are_not_compared():
    value, _ = findings_input()
    site = next(source for source in value.evidence_input.sources if source.kind == "site")
    shift = timedelta(days=31)
    site.payload.started_at -= shift
    site.payload.completed_at -= shift
    for selected in site.payload.selected_pages:
        if selected.deep_snapshot is not None:
            selected.deep_snapshot.collected_at -= shift
    site.binding.fetched_at = site.payload.completed_at
    site.binding.payload_checksum = request_digest(site.payload)
    result = builder.build_public_findings(value)
    evaluations = [item for item in result.rule_evaluations if item.rule_id in GBP_RULES]
    assert all(item.state == "not_checked" and item.reason == "comparison_time_gap" for item in evaluations)


def test_alignment_result_is_independently_rebuilt_before_findings(monkeypatch):
    value, _ = findings_input()
    original = builder.build_site_gbp_alignment

    def forged(**kwargs):
        result = original(**kwargs)
        result.alignment.fields[0].state = "mismatch"
        return result

    monkeypatch.setattr(builder, "build_site_gbp_alignment", forged)
    with pytest.raises(FindingsError, match="V22_FINDINGS_REFERENCE_INVALID"):
        builder.build_public_findings(value)
