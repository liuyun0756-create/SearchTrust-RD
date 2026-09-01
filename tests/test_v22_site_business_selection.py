import json

from app.jobs_v22.digest import request_digest
from app.report_v22.models import BusinessIdentity
from app.report_v22.site_business_facts import build_site_business_facts
from app.report_v22.site_business_selection import select_site_business_candidates
from site_business_helpers import business_html, request, two_pages


def identity(**changes):
    values = {
        "business_name": "Fixture Plumbing LLC",
        "site_url": "https://example.test/",
        "normalized_domain": "example.test",
        "operating_model": "hybrid",
        "primary_location": {
            "display_name": "Austin, TX",
            "country_code": "US",
            "city": "Austin",
            "region": "TX",
            "latitude": 30.2672,
            "longitude": -97.7431,
        },
    }
    values.update(changes)
    return BusinessIdentity.model_validate(values)


def build(value=None):
    return build_site_business_facts(value or request())


def reseal(value):
    value.source.binding.payload_checksum = request_digest(value.source.payload)
    return value


def states(result, field):
    return [item.state for item in result.eligibility if item.field == field]


def test_core_declared_entities_are_eligible_without_looking_at_gbp():
    selected = select_site_business_candidates(build(), identity())
    assert len(selected.eligibility) == 6
    assert {item.state for item in selected.eligibility} == {"eligible"}
    assert {reason for item in selected.eligibility for reason in item.reasons} == {"core_declared_entity"}
    assert all(item.evidence_ids for item in selected.eligibility)


def test_single_page_unowned_phone_and_address_stay_unresolved_but_explicit_labels_are_eligible():
    html = (
        "<h2>Business name</h2><p>Fixture Plumbing</p>"
        '<a href="tel:+12025550123">Call</a>'
        "<address>123 Main St, Austin, TX</address>"
        "<h2>Areas served</h2><ul><li>Austin</li></ul>"
    )
    selected = select_site_business_candidates(build(request(html)), identity())
    assert states(selected, "business_name") == ["eligible"]
    assert states(selected, "service_area") == ["eligible"]
    assert states(selected, "phone") == ["unresolved"]
    assert states(selected, "address") == ["unresolved"]


def test_repeated_unowned_phone_and_address_across_distinct_final_pages_become_eligible():
    html = '<a href="tel:+12025550123">Call</a><address>123 Main St, Austin, TX</address>'
    value = two_pages(html)
    value.source.payload.selected_pages[1].deep_snapshot.final_url = value.source.payload.selected_pages[1].url
    selected = select_site_business_candidates(build(reseal(value)), identity())
    assert set(states(selected, "phone")) == {"eligible"}
    assert set(states(selected, "address")) == {"eligible"}
    assert all("repeated_across_final_pages" in item.reasons for item in selected.eligibility)


def test_structured_value_corroborates_same_unowned_value_on_one_page():
    html = business_html(telephone="+1 202-555-0123", address="123 Main St, Austin, TX")
    html += '<a href="tel:+12025550123">Call</a><address>123 Main Street, Austin, TX</address>'
    selected = select_site_business_candidates(build(request(html)), identity())
    dom = [item for item in selected.eligibility if item.state == "eligible" and "structured_value_corroboration" in item.reasons]
    assert {item.field for item in dom} == {"phone", "address"}


def test_noncore_declared_record_needs_an_independent_confirmed_identity_anchor():
    value = request()
    value.source.payload.pages[0].page_type = "blog_post"
    value.source.payload.selected_pages[0].page_type = "blog_post"
    value.source.payload.selected_pages[0].deep_snapshot.page_type = "blog_post"
    value.source.payload.page_type_counts[0].label = "blog_post"
    anchored = select_site_business_candidates(build(reseal(value)), identity())
    assert {item.state for item in anchored.eligibility} == {"eligible"}
    assert all("noncore_name_anchor" in item.reasons for item in anchored.eligibility)

    raw = json.loads(business_html()[business_html().find(">") + 1:business_html().rfind("<")])
    raw["name"] = "Unrelated Publisher"
    raw["address"] = {"streetAddress": "500 Other St", "addressLocality": "Dallas"}
    raw["areaServed"] = ["Dallas"]
    value = request('<script type="application/ld+json">' + json.dumps(raw) + "</script>")
    value.source.payload.pages[0].page_type = "blog_post"
    value.source.payload.selected_pages[0].page_type = "blog_post"
    value.source.payload.selected_pages[0].deep_snapshot.page_type = "blog_post"
    value.source.payload.page_type_counts[0].label = "blog_post"
    unresolved = select_site_business_candidates(build(reseal(value)), identity())
    assert {item.state for item in unresolved.eligibility} == {"unresolved"}
    assert all("noncore_anchor_missing" in item.reasons for item in unresolved.eligibility)


def test_external_entity_hint_is_excluded_not_used_as_customer_data():
    selected = select_site_business_candidates(
        build(request(business_html(**{"@id": "https://other.test/business"}))),
        identity(),
    )
    assert {item.state for item in selected.eligibility} == {"excluded"}
    assert all(item.reasons == ["external_entity_hint"] for item in selected.eligibility)


def test_field_inspection_requires_a_complete_home_but_invalid_value_alone_is_checked():
    invalid = select_site_business_candidates(
        build(request(business_html(name=" " * 2, telephone=None, address=None, areaServed=None))),
        identity(),
    )
    name = next(item for item in invalid.inspections if item.field == "business_name")
    assert name.complete is True and name.invalid_value_observed is True

    broken = select_site_business_candidates(build(request("<address>Unclosed")), identity())
    assert all(item.complete is False for item in broken.inspections)


def test_selection_is_input_order_independent():
    facts = build()
    first = select_site_business_candidates(facts, identity())
    facts.candidates.reverse()
    facts.pages.reverse()
    facts.evidence_index.reverse()
    second = select_site_business_candidates(facts, identity())
    assert first == second
