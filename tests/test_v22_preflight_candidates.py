import json
from dataclasses import replace
from pathlib import Path

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
