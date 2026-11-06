import gzip
import json
from dataclasses import replace
from pathlib import Path

import pytest

from app.api.v2.models import PreflightRequest
from app.preflight_v22.candidates import build_candidates
from app.preflight_v22.extractors import extract_site_signals
from app.preflight_v22.gbp import GbpCandidate, GbpLookupResult
from app.report_v22.models import TargetMarket


FIXTURES = Path(__file__).parent / "fixtures" / "v22_preflight"


def request_with_user_context() -> PreflightRequest:
    return PreflightRequest.model_validate_json(json.dumps({
        "site_url": "https://example.com",
        "primary_service": "Emergency Plumbing",
        "target_market": {
            "display_name": "Austin, TX, US",
            "country_code": "US",
            "region": "TX",
            "city": "Austin",
        },
    }))


def complete_signals():
    return extract_site_signals((FIXTURES / "homepage_complete.html").read_text())


def gbp_candidate(*, city: str = "Austin", website_url: str = "https://example.com/") -> GbpCandidate:
    return GbpCandidate(
        business_name="Acme Plumbing",
        website_url=website_url,
        public_gbp_url="https://www.google.com/maps?cid=1311768467294899695",
        phone="+1 512-555-0100" if city == "Austin" else "+1 214-555-0199",
        address=f"100 Main St, {city}, TX 78701",
        market=TargetMarket(
            display_name=f"{city}, TX, US",
            country_code="US",
            region="TX",
            city=city,
        ),
        operating_model="storefront",
        categories=("Plumber",),
    )


def test_candidate_builder_prioritizes_user_inputs_and_merges_matching_gbp() -> None:
    result = build_candidates(
        request=request_with_user_context(),
        normalized_site_url="https://example.com/",
        signals=complete_signals(),
        gbp_result=GbpLookupResult("found", "GBP_FOUND", "GBP candidates found.", (gbp_candidate(),)),
    )

    assert result.services[0].value == "Emergency Plumbing"
    assert result.services[0].confidence == "high"
    assert result.services[1].value == "Residential Plumbing"
    assert result.markets[0].market.city == "Austin"
    assert result.identities[0].business.business_name == "Acme Plumbing"
    assert result.identities[0].business.operating_model == "hybrid"
    assert result.identities[0].business.public_gbp_url is not None
    assert result.identities[0].confidence == "high"
    assert result.identities[0].requires_confirmation is False
    assert "website domain matched" in result.identities[0].match_reasons
    comparisons = {item.field: item for item in result.identities[0].field_comparisons}
    assert set(comparisons) == {"business_name", "phone", "address", "service_area"}
    assert comparisons["business_name"].status == "exact_match"
    assert comparisons["phone"].status == "exact_match"
    assert comparisons["address"].status in {"exact_match", "partial_match"}
    assert comparisons["service_area"].status == "partial_match"


def test_candidate_comparisons_distinguish_partial_missing_and_error() -> None:
    signals = complete_signals()
    result = build_candidates(
        request=request_with_user_context(),
        normalized_site_url="https://example.com/",
        signals=replace(signals, phones=()),
        gbp_result=GbpLookupResult(
            "found",
            "GBP_FOUND",
            "GBP candidates found.",
            (replace(gbp_candidate(), business_name="Acme Plumbing LLC", phone=None),),
        ),
    )

    comparisons = {item.field: item for item in result.identities[0].field_comparisons}
    assert comparisons["business_name"].status == "partial_match"
    assert comparisons["phone"].status == "error"


def test_candidate_comparison_treats_one_sided_value_as_not_matched() -> None:
    signals = complete_signals()
    result = build_candidates(
        request=request_with_user_context(),
        normalized_site_url="https://example.com/",
        signals=signals,
        gbp_result=GbpLookupResult(
            "found",
            "GBP_FOUND",
            "GBP candidates found.",
            (replace(gbp_candidate(), phone=None),),
        ),
    )

    phone = next(
        item for item in result.identities[0].field_comparisons if item.field == "phone"
    )
    assert phone.site_value is not None
    assert phone.gbp_value is None
    assert phone.status == "not_matched"


def test_candidate_builder_marks_multiple_similar_gbp_candidates_for_confirmation() -> None:
    result = build_candidates(
        request=request_with_user_context(),
        normalized_site_url="https://example.com/",
        signals=complete_signals(),
        gbp_result=GbpLookupResult(
            "found",
            "GBP_FOUND",
            "GBP candidates found.",
            (gbp_candidate(), gbp_candidate(city="Dallas", website_url="https://other.example/")),
        ),
    )

    assert len(result.identities) == 2
    assert all(candidate.requires_confirmation for candidate in result.identities)


def test_candidate_builder_does_not_create_placeholder_identity() -> None:
    request = PreflightRequest.model_validate_json(json.dumps({"site_url": "https://example.com"}))
    signals = extract_site_signals((FIXTURES / "homepage_ambiguous.html").read_text())

    result = build_candidates(
        request=request,
        normalized_site_url="https://example.com/",
        signals=signals,
        gbp_result=GbpLookupResult("not_found", "GBP_NOT_FOUND", "No GBP candidate was found.", ()),
    )

    assert result.identities == ()
    assert result.markets == ()


@pytest.mark.parametrize("site_id,expected_phone", [
    ("all-star-plumbing-fresno", "+1-559-445-9999"),
    ("pr-plumbing-fresno", "559-244-0440"),
    ("seamless-gutters-pensacola", "+18507761782"),
    ("vision-tech-swansea", "03333355510"),
    ("volcanoes-hiking-guides", None),
])
def test_frozen_homepage_phone_identity_regression(site_id, expected_phone):
    directory = FIXTURES.parent / "gold" / site_id
    saved = json.loads(gzip.decompress((directory / "bound-p0-10.json.gz").read_bytes()))["request"]
    sources = saved["evidence_input"]["sources"]
    inventory = next(s["payload"] for s in sources if s["kind"] == "site")
    homepage = next(p for p in inventory["selected_pages"] if p["url"].rstrip("/") == inventory["root_url"].rstrip("/"))
    # Volcanoes has no deep snapshot in this frozen collection. Do not replace
    # unavailable evidence with its separate, later standalone page.html capture.
    deep = homepage["deep_snapshot"]
    if expected_phone is None:
        assert site_id == "volcanoes-hiking-guides" and deep is None
    else:
        assert deep and deep["html"]
    signals = extract_site_signals(deep["html"] if deep else "")
    record = next(s["payload"]["record"] for s in sources if s["kind"] == "public_gbp")
    fields = record["fields"]
    assert fields["phone"]["state"] == "observed"
    market = TargetMarket.model_validate(saved["evidence_input"]["context"]["target_market"])
    identity = saved["business_identity"]
    gbp = GbpCandidate(
        business_name=fields["business_name"]["value"], website_url=identity["site_url"],
        public_gbp_url=record["observed_public_gbp_url"], phone=fields["phone"]["value"],
        address=fields["address"]["value"], market=market,
        operating_model=identity["operating_model"], categories=(),
    )
    result = build_candidates(
        request=PreflightRequest(site_url=identity["site_url"], target_market=market),
        normalized_site_url=identity["site_url"], signals=signals,
        gbp_result=GbpLookupResult("found", "GBP_FOUND", "Frozen GBP record", (gbp,)),
    )
    candidate = result.identities[0]
    phone = next(c for c in candidate.field_comparisons if c.field == "phone")
    assert phone.site_value == expected_phone
    assert phone.gbp_value == gbp.phone
    assert phone.status == ("exact_match" if expected_phone else "not_matched")
    assert ("phone number matched" in candidate.match_reasons) == (expected_phone is not None)
    if expected_phone is None:
        assert signals.phones == ()  # Not observed, not an observed phone mismatch.


def test_phone_array_ignores_non_strings_and_displays_matching_later_item():
    document = '<script type="application/ld+json">' + json.dumps({
        "@type": "LocalBusiness", "name": "Acme Plumbing",
        "telephone": [None, 5125550100, False, {"telephone": "bad"}, ["bad"], " ",
                      "+1 214-555-0199", " +1 512-555-0100 "],
    }) + '</script>'
    phones = extract_site_signals(document).phones
    assert [p.value for p in phones] == ["+1 214-555-0199", "+1 512-555-0100"]
    result = build_candidates(
        request=request_with_user_context(), normalized_site_url="https://example.com/",
        signals=replace(complete_signals(), phones=phones),
        gbp_result=GbpLookupResult("found", "GBP_FOUND", "GBP candidates found.", (gbp_candidate(),)),
    )
    candidate = result.identities[0]
    phone = next(c for c in candidate.field_comparisons if c.field == "phone")
    assert phone.status == "exact_match"
    assert phone.site_value == "+1 512-555-0100"
    assert "phone number matched" in candidate.match_reasons
