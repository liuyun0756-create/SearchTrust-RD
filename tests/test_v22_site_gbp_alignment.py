import json

from app.report_v22.evidence import build_evidence_index
from app.report_v22.models import BusinessIdentity
from app.report_v22.site_business_facts import build_site_business_facts
from app.report_v22.site_business_selection import select_site_business_candidates
from app.report_v22.site_gbp_alignment import build_site_gbp_alignment
from public_gbp_helpers import evidence_input, sample_input
from site_business_helpers import business_html, request


def exact_html(**changes):
    values = {
        "name": "Fixture Plumbing",
        "telephone": "+1 512 555 0100",
        "address": "123 Fixture St, Austin, TX",
        "areaServed": ["Austin", "Round Rock"],
    }
    values.update(changes)
    return business_html(**values)


def exact_gbp():
    raw = sample_input()
    fields = raw["record"]["fields"]
    fields["business_name"]["value"] = "Fixture Plumbing"
    fields["address"]["value"] = "123 Fixture St, Austin, TX"
    fields["phone"]["value"] = "+1 512 555 0100"
    fields["service_areas"]["value"] = ["Austin", "Round Rock"]
    fields["service_area_business"]["value"] = True
    return raw


def identity(inputs, *, operating_model="hybrid"):
    return BusinessIdentity(
        business_name="Fixture Plumbing LLC",
        site_url=inputs.context.site_url,
        normalized_domain="example.test",
        operating_model=operating_model,
        primary_location=inputs.context.target_market,
        public_gbp_url=inputs.context.customer_public_gbp.public_gbp_url,
    )


def align(*, html=None, raw=None, operating_model="hybrid"):
    raw = raw or exact_gbp()
    site_request = request(html if html is not None else exact_html())
    inputs = evidence_input(raw, site_request.source)
    evidence = build_evidence_index(inputs)
    facts = build_site_business_facts(site_request)
    confirmed = identity(inputs, operating_model=operating_model)
    selection = select_site_business_candidates(facts, confirmed)
    public_source = next(source for source in inputs.sources if source.kind == "public_gbp")
    site_source = next(source for source in inputs.sources if source.kind == "site")
    return build_site_gbp_alignment(
        context=inputs.context,
        identity=confirmed,
        facts=facts,
        selection=selection,
        site_source=site_source,
        public_source=public_source,
        evidence=evidence,
    )


def states(result):
    return {item.field: item.state for item in result.alignment.fields}


def missing(raw, *fields):
    for field in fields:
        key = "service_areas" if field == "service_area" else field
        raw["record"]["fields"][key] = {"state": "not_returned", "value": [] if key == "service_areas" else None}
    return raw


def test_all_four_fields_can_be_exactly_aligned():
    result = align()
    assert states(result) == {
        "address": "exact_match",
        "business_name": "exact_match",
        "phone": "exact_match",
        "service_area": "exact_match",
    }
    assert result.evidence_index == build_site_business_facts(request(exact_html())).evidence_index


def test_any_matching_site_candidate_aligns_while_unmatched_extras_remain_disclosed():
    records = [
        {"@context": "https://schema.org", "@type": "LocalBusiness", "name": "Other Trading Name"},
        {"@context": "https://schema.org", "@type": "LocalBusiness", "name": "Fixture Plumbing"},
    ]
    html = '<script type="application/ld+json">' + json.dumps(records) + "</script>"
    field = next(item for item in align(html=html).alignment.fields if item.field == "business_name")
    assert field.state == "exact_match"
    assert field.matched_pairs and field.unmatched_site_ids


def test_single_and_double_missing_are_findable_states_with_dedicated_coverage_evidence():
    site_missing = align(html=exact_html(telephone=None))
    assert states(site_missing)["phone"] == "site_missing"
    gbp_missing = align(raw=missing(exact_gbp(), "phone"))
    assert states(gbp_missing)["phone"] == "gbp_missing"
    both = align(html=exact_html(telephone=None), raw=missing(exact_gbp(), "phone"))
    phone = next(item for item in both.alignment.fields if item.field == "phone")
    assert phone.state == "both_missing" and len(phone.coverage_evidence_ids) == 2
    coverage = [item for item in both.evidence_index if item.evidence_id in phone.coverage_evidence_ids]
    assert {item.original_value for item in coverage} == {"site_missing", "gbp_missing"}
    traces = {item.evidence_id: item for item in both.source_traces}
    assert all(trace.selector.category == "coverage" for trace in (traces[item.evidence_id] for item in coverage))
    assert all(trace.selector.record_context[0] == "site_gbp_alignment_v1" for trace in (traces[item.evidence_id] for item in coverage))


def test_service_area_partial_and_mismatch_are_distinct():
    raw = exact_gbp()
    raw["record"]["fields"]["service_areas"]["value"] = ["Austin", "Pflugerville"]
    assert states(align(raw=raw))["service_area"] == "partial_match"
    raw = exact_gbp()
    raw["record"]["fields"]["service_areas"]["value"] = ["Dallas"]
    assert states(align(raw=raw))["service_area"] == "mismatch"


def test_operating_model_controls_address_and_service_area_applicability():
    assert states(align(operating_model="storefront"))["service_area"] == "not_applicable"
    assert states(align(operating_model="service_area"))["address"] == "not_applicable"


def test_only_unresolved_site_values_are_not_reported_as_missing():
    html = '<a href="tel:+15125550100">Call</a>'
    field = next(item for item in align(html=html).alignment.fields if item.field == "phone")
    assert field.state == "not_checked" and field.not_checked_reason == "identity_unresolved"


def test_ineligible_public_source_is_not_reported_as_missing_or_mismatch():
    raw = exact_gbp()
    raw["expires_at"] = raw["completed_at"] + __import__("datetime").timedelta(minutes=1)
    result = align(raw=raw)
    assert all(item.state == "not_checked" and item.not_checked_reason == "source_ineligible"
               for item in result.alignment.fields)
