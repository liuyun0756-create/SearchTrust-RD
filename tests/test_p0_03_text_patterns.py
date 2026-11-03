"""v6 positive-only text corroboration, with v1 defaults deliberately retained."""
from pathlib import Path
import json

import pytest

from app.jobs_v22.digest import canonical_json_bytes, request_digest
from app.report_v22.evidence import build_evidence_index
from app.report_v22.findings import build_public_findings
from app.report_v22.findings_models import PublicFindingsInput
from app.report_v22.site_business_errors import SiteBusinessError
from app.report_v22.site_business_facts import build_site_business_facts
from app.report_v22.site_business_selection import select_site_business_candidates
from app.report_v22.site_gbp_alignment import build_site_gbp_alignment, verify_site_gbp_alignment
from public_gbp_helpers import evidence_input
from site_business_helpers import request, business_html, two_pages
from test_v22_site_gbp_alignment import exact_gbp, identity

FIXTURES = Path(__file__).parent / "fixtures/p0_03"


def run(html, *, phone="+1 512 555 0100", address="123 Fixture Street, Austin, TX 78701", country="US", enabled=True, pages=None):
    site = pages or request(html.strip())
    raw = exact_gbp()
    fields = raw["record"]["fields"]
    for name, value in (("phone", phone), ("address", address)):
        fields[name] = {"state": "observed" if value else "not_returned", "value": value}
    inputs = evidence_input(raw, site.source)
    inputs.context.target_market.country_code = country
    confirmed = identity(inputs)
    facts = build_site_business_facts(site, text_patterns=enabled, gbp_phone=phone, gbp_address=address, country=country)
    selection = select_site_business_candidates(facts, confirmed)
    args = dict(context=inputs.context, identity=confirmed, facts=facts, selection=selection,
                site_source=site.source, public_source=inputs.sources[0], evidence=build_evidence_index(inputs))
    result = build_site_gbp_alignment(**args)
    verify_site_gbp_alignment(result, **args)
    findings_input = PublicFindingsInput(evidence_input=inputs, business_identity=confirmed)
    return facts, selection, result, findings_input


def field(result, name):
    return next(item for item in result.alignment.fields if item.field == name)


def test_footer_text_matches_without_claiming_independent_eligibility():
    html = (FIXTURES / "footer.html").read_text()
    old = run(html, enabled=False)[2]
    facts, selection, result, inputs = run(html)
    assert field(old, "phone").state == field(old, "address").state == "site_missing"
    assert field(result, "phone").state == field(result, "address").state == "semantic_match"
    assert facts.extraction_version == "site_business_extraction_v2"
    assert facts.schema_version == "site_business_facts_v2"
    assert result.alignment.alignment_version == "site_gbp_alignment_v2"
    assert all(item.state == "unresolved" for item in selection.eligibility)
    assert all("text recognition" in " ".join(field(result, name).limitations) for name in ("phone", "address"))
    assert any(c.field == "opening_hours" for c in facts.candidates)
    assert {f.field for f in result.alignment.fields} == {"phone", "address", "business_name", "service_area"}
    findings = build_public_findings(inputs, text_patterns=True, reviewed_evidence=True)
    assert not any(f.rule_id.endswith(("phone_alignment", "address_alignment")) for f in findings.findings)
    assert all(e.rule_version == "1.2.0" for e in findings.rule_evaluations if e.rule_id.endswith("_alignment"))


@pytest.mark.parametrize("number,gbp,country", [
    ("(555) 123-4567", "5551234567", "US"), ("555-123-4567", "+1 555 123 4567", "US"),
    ("+1 555 123 4567", "5551234567", "US"), ("01792 123456", "+44 1792 123456", "GB"),
    ("+44 (0)1792 123456", "01792 123456", "GB"), ("+49 30 12345678", "+49 30 12345678", "DE"),
])
def test_phone_formats_and_gbp_digit_sequence(number, gbp, country):
    facts, _, result, _ = run(f"<p>Call us {number}</p>", phone=gbp, country=country)
    assert any(c.scalar_value == number for c in facts.candidates)
    assert field(result, "phone").state == "semantic_match"


@pytest.mark.parametrize("html", ["<p>Fax 512-555-0199</p>", "<p>No contact information in this sample</p>"])
def test_nonmatching_or_unobserved_phone_is_not_checked_never_mismatch(html):
    _, _, result, inputs = run(html)
    phone = field(result, "phone")
    assert (phone.state, phone.not_checked_reason) == ("not_checked", "site_content_not_checked")
    findings = build_public_findings(inputs, text_patterns=True, reviewed_evidence=True)
    evaluation = next(e for e in findings.rule_evaluations if e.rule_id.endswith("phone_alignment"))
    assert (evaluation.state, evaluation.reason) == ("not_checked", "site_content_not_checked")
    assert evaluation.finding_id is None


@pytest.mark.parametrize("wrapper", [
    '<script>{}</script>', '<style>{}</style>', '<template>{}</template>', '<noscript>{}</noscript>',
    '<div hidden><span>{}</span></div>', '<div aria-hidden="true"><span>{}</span></div>',
    '<div style="display: none"><span>{}</span></div>', '<div style="visibility:hidden">{}</div>',
    '<head><title>{}</title></head>',
])
def test_nonvisible_content_is_not_text_evidence(wrapper):
    facts, _, result, _ = run(wrapper.format("Call 512-555-0100 at 123 Fixture Street"))
    assert not any(c.source_kind == "text_pattern" for c in facts.candidates)
    assert field(result, "phone").state == "not_checked"


def test_text_cannot_override_structured_mismatch_or_unresolved_value():
    text = "<p>Call 512-555-0100</p>"
    _, _, result, _ = run(business_html(telephone="+1 512 555 0199") + text)
    assert field(result, "phone").state == "mismatch"
    _, _, result, _ = run('<a href="tel:+15125550199">Call</a>' + text)
    assert (field(result, "phone").state, field(result, "phone").not_checked_reason) == ("not_checked", "identity_unresolved")


def test_repeated_text_still_requires_gbp_and_cannot_become_eligible():
    pages = two_pages("<p>Fax 512-555-0199</p>")
    pages.source.payload.selected_pages[1].deep_snapshot.final_url = pages.source.payload.selected_pages[1].url
    pages.source.binding.payload_checksum = request_digest(pages.source.payload)
    _, selection, result, _ = run("", pages=pages)
    assert all(e.state == "unresolved" for e in selection.eligibility)
    assert field(result, "phone").state == "not_checked"


def test_address_requires_gbp_and_postcode_is_only_partial_corroboration():
    html = "<footer><p>Visit us: SA14 6EF</p></footer>"
    facts, _, result, _ = run(html, address=None, country="GB")
    assert not any(c.field == "address" for c in facts.candidates)
    assert field(result, "address").state == "not_checked"
    _, _, result, inputs = run(html, address="Unit 9, Parc Gelli Werdd, Llanelli SA14 6EF", country="GB")
    address = field(result, "address")
    assert address.state == "partial_match" and address.matched_pairs[0].compared_components == ["postal"]
    findings = build_public_findings(inputs, text_patterns=True, reviewed_evidence=True)
    evaluation = next(e for e in findings.rule_evaluations if e.rule_id.endswith("address_alignment"))
    assert evaluation.state == "not_triggered" and evaluation.finding_id is None


def test_unmatched_address_text_never_becomes_missing_or_mismatch():
    _, _, result, _ = run("<p>987 Another Road, Dallas TX 75201</p>")
    assert (field(result, "address").state, field(result, "address").not_checked_reason) == ("not_checked", "site_content_not_checked")


def test_text_evidence_provenance_and_budgets(monkeypatch):
    from app.report_v22 import site_business_evidence as evidence
    html = "<p>Call <span>512</span>-555-0100</p>"
    facts, _, result, _ = run(html)
    phone = next(c for c in facts.candidates if c.field == "phone")
    assert phone.origins[0].excerpt == html
    assert facts.evidence_index[0].original_value == "512-555-0100"
    assert facts.source_traces[0].selector.record_context[0] == "site_business_extraction_v2"
    original = evidence.make_candidate
    def forged(*args, **kwargs):
        value = original(*args, **kwargs)
        value.scalar_value = "512-555-0199"
        return value
    monkeypatch.setattr(evidence, "make_candidate", forged)
    with pytest.raises(SiteBusinessError, match="REFERENCE_INVALID"):
        run(html)
    monkeypatch.setattr(evidence, "make_candidate", original)
    site = request("<p>512-555-0100</p><p>512-555-0199</p>")
    site.limits.max_candidates_per_page = 1
    with pytest.raises(SiteBusinessError, match="LIMIT_EXCEEDED"):
        build_site_business_facts(site, text_patterns=True)


@pytest.mark.parametrize("path", sorted((FIXTURES / "legacy").glob("*.input.json")), ids=lambda p: p.stem)
def test_frozen_v3_v4_v5_reports_match_pre_change_bytes(path):
    from app.report_v22.capability_public import build_public_capability_report
    raw = json.loads(path.read_bytes())
    before = canonical_json_bytes(raw)
    report = build_public_capability_report(raw["manifest"], raw["request"])
    assert canonical_json_bytes(report) == path.with_name(path.name.replace(".input.json", ".report.json")).read_bytes()
    assert canonical_json_bytes(raw) == before


def test_real_visiontech_static_capture_records_text_without_overriding_tel_link():
    import hashlib
    html = (FIXTURES / "visiontech-swansea.html").read_bytes()
    meta = json.loads((FIXTURES / "visiontech-swansea.source.json").read_text())
    assert hashlib.sha256(html).hexdigest() == meta["sha256"]
    kwargs = dict(phone="0333 335 5510", address="Unit 9, Parc Gelli Werdd, Cross Hands, Llanelli SA14 6EF", country="GB")
    old = run(html.decode(), enabled=False, **kwargs)[2]
    facts, _, new, _ = run(html.decode(), **kwargs)
    assert field(old, "address").state == "site_missing"
    assert field(new, "address").state == "partial_match"
    assert any(c.source_kind == "text_pattern" and c.scalar_value == "0333 335 5510" for c in facts.candidates)
    assert any(c.field == "opening_hours" and c.scalar_value == "Mon - Fri: 09:00 - 17:30" for c in facts.candidates)
    # The existing, unresolved tel: candidate takes precedence over text fallback.
    assert field(new, "phone").state == "not_checked"
    assert field(new, "phone").not_checked_reason == "identity_unresolved"


def test_complete_capability_route_only_enables_text_for_v6():
    from app.report_v22.capability_public import RULESET, build_public_capability_report
    from site_business_helpers import source
    from test_v22_capability_public import inputs
    html = (FIXTURES / "footer.html").read_text().strip()
    for version in ("v22_capability_public_v3", "v22_capability_public_v4", "v22_capability_public_v5", RULESET):
        manifest, request = inputs(business_source=source(html), ruleset=version)
        before = canonical_json_bytes(request)
        report = build_public_capability_report(manifest, request)
        assert any(e.value == "Mon - Fri: 09:00 - 17:30" for e in report.evidence) is (version == RULESET)
        assert canonical_json_bytes(request) == before
        if version == RULESET:
            assert not any(f.rule_id.endswith(("phone_alignment", "address_alignment")) for f in report.findings)


def test_v6_exported_report_matches_actual_computation():
    from app.report_v22.capability_public import build_public_capability_report
    from site_business_helpers import source
    from test_v22_capability_public import inputs
    manifest, request = inputs(business_source=source((FIXTURES / "footer.html").read_text().strip()))
    assert canonical_json_bytes(build_public_capability_report(manifest, request)) == (FIXTURES / "v6-footer.report.json").read_bytes()
