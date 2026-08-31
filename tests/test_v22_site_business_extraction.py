import json

import pytest

from app.report_v22.site_business_facts import build_site_business_facts
from site_business_helpers import business_html, request


def build(html):
    return build_site_business_facts(request(html))


def script(value):
    return '<script type="application/ld+json">' + json.dumps(value, ensure_ascii=False) + '</script>'


def values(result, field):
    return sorted(c.scalar_value for c in result.candidates if c.field == field and c.scalar_value is not None)


def test_structured_values_are_candidates_not_confirmed_primary_values():
    result = build(business_html())
    assert len(result.candidates) == 6
    assert len(result.evidence_index) == 7
    assert values(result, "business_name") == [" Fixture Plumbing "]
    assert values(result, "phone") == ["+1 512 555 0100", "+1 512 555 0101"]
    assert values(result, "service_area") == ["Austin", "Round Rock"]
    address = next(c for c in result.candidates if c.field == "address")
    assert address.scalar_value is None
    assert address.components == {"streetAddress": "123 Fixture St", "addressLocality": "Austin"}
    assert all(c.ownership_status == "declared_entity" for c in result.candidates)
    assert all(i.original_value == i.normalized_value for i in result.evidence_index)
    assert next(i for i in result.evidence_index if i.original_value == " Fixture Plumbing ")
    assert result.pages[0].status == "parsed"


@pytest.mark.parametrize("wrapper", [lambda r: r, lambda r: [r], lambda r: {"@context": "https://schema.org/", "@graph": [r]}])
def test_supported_record_containers(wrapper):
    result = build(script(wrapper({"@type": "https://schema.org/Plumber", "name": "Plumber A"})))
    assert values(result, "business_name") == ["Plumber A"]


@pytest.mark.parametrize("kind", ["Organization", "LocalBusiness", "Plumber", "HomeAndConstructionBusiness", "ProfessionalService", "Electrician", "HVACBusiness", "RoofingContractor", "GeneralContractor", "Locksmith", "MovingCompany"])
def test_whitelisted_types(kind):
    assert values(build(script({"@type": ["Thing", kind], "name": "A"})), "business_name") == ["A"]


@pytest.mark.parametrize("record", [
    {"@type": "Article", "author": {"@type": "LocalBusiness", "name": "Not promoted"}},
    {"@type": "https://other.test/Plumber", "name": "Not a whitelisted type"},
    {"@type": "LocalBusiness", "@id": "#reference"},
])
def test_no_nested_promotion_type_suffix_or_reference_loading(record):
    result = build(script(record))
    assert not result.candidates
    assert all(f.observation_status == "not_observed" for f in result.pages[0].fields.values())


@pytest.mark.parametrize("raw,code", [
    ('{"@type":"LocalBusiness","name":"A","name":"B"}', "duplicate_json_key"),
    ('{"@type":"LocalBusiness","name":NaN}', "jsonld_parse_failed"),
    ('{"@type":"LocalBusiness","name":Infinity}', "jsonld_parse_failed"),
    ('{"@type":"LocalBusiness","name":1e999}', "jsonld_parse_failed"),
    ('{broken', "jsonld_parse_failed"),
    ('{"@type":"LocalBusiness","name":"\\ud800"}', "jsonld_parse_failed"),
    ('{"@type":"LocalBusiness","unused":123456789012345678901234567890}', "jsonld_parse_failed"),
])
def test_bad_json_block_keeps_other_observations(raw, code):
    result = build('<script type="application/ld+json">' + raw + '</script><a href="tel:555-0100">Call</a>')
    assert values(result, "phone") == ["555-0100"]
    assert result.pages[0].status == "partial"
    assert code in {d.code for d in result.pages[0].diagnostics}
    assert result.pages[0].fields["business_name"].observation_status == "not_checked"


@pytest.mark.parametrize("context", [{"name": "alias"}, "https://other.test/context", None, ["https://schema.org", "https://other.test/"]])
def test_unsupported_context_never_supplies_structured_values(context):
    result = build(business_html(**{"@context": context}))
    assert not result.candidates
    assert result.pages[0].diagnostics[0].code == "unsupported_context"
    assert all(f.observation_status == "not_checked" for f in result.pages[0].fields.values())


def test_multiple_entities_addresses_contact_points_and_area_phrases():
    result = build(script([
        {"@type": "LocalBusiness", "name": "A", "telephone": ["555-1", "555-2"],
         "contactPoint": [{"telephone": ["555-3", "555-4"]}],
         "address": [{"streetAddress": "First", "postalCode": "100"}, {"streetAddress": "Second", "postalCode": "100"}, " Raw Address "],
         "areaServed": ["A, B and C / D", {"name": " E "}], "serviceArea": {"name": "F"}},
        {"@type": "LocalBusiness", "name": "B", "telephone": "555-1"},
    ]))
    assert values(result, "business_name") == ["A", "B"]
    assert values(result, "phone") == ["555-1", "555-1", "555-2", "555-3", "555-4"]
    assert values(result, "service_area") == [" E ", "A, B and C / D", "F"]
    addresses = [c for c in result.candidates if c.field == "address"]
    assert len(addresses) == 3
    assert len(set(i for c in addresses for i in c.evidence_ids)) == 5
    assert all("primary" not in c.model_dump() and "conflict" not in c.model_dump() for c in result.candidates)


def test_null_array_entries_and_empty_service_names_are_absent():
    result = build(script({"@type": "LocalBusiness", "telephone": [None, [], "555"],
                           "address": [None, []], "contactPoint": [None, []],
                           "areaServed": [{"name": None}, {"name": []}]}))
    assert values(result, "phone") == ["555"]
    assert result.pages[0].status == "parsed"


@pytest.mark.parametrize("document", [True, 42, "LocalBusiness", [None], {"@graph": {"@type": "LocalBusiness", "name": "A"}}])
def test_unsupported_json_containers_do_not_claim_complete_inspection(document):
    result = build(script(document))
    assert not result.candidates
    assert result.pages[0].status == "partial"
    assert all(f.observation_status == "not_checked" for f in result.pages[0].fields.values())


def test_partial_address_keeps_valid_components_without_fake_joining():
    result = build(business_html(address={"streetAddress": " Street ", "addressLocality": 123,
                                         "addressCountry": {"name": " US "}, "unknown": "Ignored"}))
    candidate = next(c for c in result.candidates if c.field == "address")
    assert candidate.components == {"streetAddress": " Street ", "addressCountry": " US "}
    assert candidate.scalar_value is None
    assert result.pages[0].fields["address"].observation_status == "observed"
    assert result.pages[0].status == "partial"


@pytest.mark.parametrize("property_name,field", [("name", "business_name"), ("telephone", "phone"), ("address", "address"), ("areaServed", "service_area")])
@pytest.mark.parametrize("value", [None, []])
def test_null_and_empty_fields_are_absent_not_failed(property_name, field, value):
    result = build(script({"@type": "LocalBusiness", property_name: value}))
    assert not result.candidates
    assert result.pages[0].status == "parsed"
    assert result.pages[0].fields[field].observation_status == "not_observed"


@pytest.mark.parametrize("property_name,field,bad", [("name", "business_name", " " * 2), ("name", "business_name", "X" * 241), ("telephone", "phone", "no number"), ("telephone", "phone", 555), ("address", "address", "A" * 501), ("areaServed", "service_area", "A" * 241)])
def test_invalid_values_are_not_truncated_or_coerced(property_name, field, bad):
    result = build(script({"@type": "LocalBusiness", property_name: bad}))
    assert not result.candidates
    assert result.pages[0].fields[field].observation_status == "not_checked"
    assert all(status.observation_status == "not_observed" for key, status in result.pages[0].fields.items() if key != field)


@pytest.mark.parametrize("hint", ["https://other.test/company", "urn:business:123", 123, "x" * 2084])
def test_unresolved_hints_do_not_erase_actual_name(hint):
    result = build(script({"@type": "LocalBusiness", "@id": hint, "name": " A "}))
    assert values(result, "business_name") == [" A "]
    assert result.candidates[0].ownership_status == "unresolved"
    assert result.pages[0].status == "parsed"


def test_dom_explicit_fields_preserve_entities_whitespace_unicode_and_br():
    result = build('中文前缀\n<dl><dt>  BUSINESS   NAME：</dt><dd> A &amp; B </dd></dl>'
                   '<table><tr><th>电话:</th><td> +86 555 0100 </td></tr></table>'
                   '<address>一号<br>二号 &amp; 三号<span hidden>秘密</span></address>'
                   '<h2>Areas served</h2><ul><li>Austin</li><li> Round Rock </li></ul>'
                   '<h3>We serve:</h3><p>A, B and C / D</p><a href="TEL:555-999&amp;ext=2">Different display</a>')
    assert values(result, "business_name") == [" A & B "]
    assert values(result, "address") == ["一号\n二号 & 三号"]
    assert values(result, "phone") == [" +86 555 0100 ", "555-999&ext=2"]
    assert values(result, "service_area") == [" Round Rock ", "A, B and C / D", "Austin"]
    assert all(c.entity_key is None and c.ownership_status == "unresolved" for c in result.candidates)


@pytest.mark.parametrize("wrapper", ["<script>{}</script>", "<style>{}</style>", "<noscript>{}</noscript>", "<template>{}</template>", "<svg>{}</svg>", "<div hidden>{}</div>", '<div aria-hidden="true">{}</div>'])
def test_hidden_and_noncontent_subtrees_never_supply_dom_facts(wrapper):
    result = build(wrapper.format('<address>Private address</address><a href="tel:555">Call</a>'))
    assert not result.candidates


def test_no_title_logo_full_text_or_saved_text_fallback():
    result = build('<title>Business title</title><h1>Brand</h1><img alt="Logo"><p>Phone 555-0100. Austin, TX.</p>')
    assert not result.candidates
    assert all(f.observation_status == "not_observed" for f in result.pages[0].fields.values())


def test_duplicate_records_merge_unique_origins_but_not_changed_entities():
    record = {"@type": "LocalBusiness", "name": "A"}
    result = build(script(record) + script(record) + script({**record, "@id": "#other"}))
    assert values(result, "business_name") == ["A", "A"]
    assert sorted(len(c.origins) for c in result.candidates) == [1, 2]


def test_malformed_html_does_not_erase_closed_phone_link():
    result = build('<a href="tel:555">Call</a><address>Unclosed')
    assert values(result, "phone") == ["555"]
    assert not values(result, "address")
    assert result.pages[0].status == "partial"
    assert result.pages[0].fields["address"].observation_status == "not_checked"


def test_double_colon_label_does_not_match_single_colon_rule():
    assert not build('<h2>Phone：:</h2><p>555</p>').candidates


@pytest.mark.parametrize("html", [
    '<h2>Phone</h2><div><h3>Someone else</h3><p>555</p></div>',
    '<dl><dt>Phone</dt><dd><dl><dt>Other phone</dt><dd>555</dd></dl></dd></dl>',
    '<h2>Areas served</h2><ul><li>Region<ul><li>Child area</li></ul></li></ul>',
])
def test_ambiguous_nested_label_values_are_not_concatenated(html):
    result = build(html)
    assert not result.candidates
    assert any(d.code == "unsupported_field_shape" for d in result.pages[0].diagnostics)


def test_only_invalid_structured_channel_and_broken_dom_is_parse_failed():
    result = build(script({"@context": "https://other.test/", "@type": "LocalBusiness", "name": "A"}) + '<address>Broken')
    assert result.pages[0].status == "parse_failed"
