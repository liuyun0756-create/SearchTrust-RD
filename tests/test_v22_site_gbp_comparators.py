import pytest

from app.report_v22.site_gbp_comparators import (
    compare_addresses,
    compare_business_names,
    compare_phones,
    compare_service_areas,
    normalize_address,
    normalize_business_name,
    normalize_phone,
)


@pytest.mark.parametrize("left,right,expected", [
    ("Fixture Plumbing", "Fixture Plumbing", "exact_match"),
    ("Fixture Plumbing, LLC", "fixture plumbing", "semantic_match"),
    ("Smith & Sons Inc.", "SMITH AND SONS", "semantic_match"),
    ("Fixture Plumbing Austin", "Fixture Plumbing", "mismatch"),
    ("Fixture Plumbing 2", "Fixture Plumbing", "mismatch"),
])
def test_business_name_comparison_is_controlled_not_fuzzy(left, right, expected):
    assert compare_business_names(left, right, "US").state == expected


def test_business_name_normalization_records_only_applied_transformations():
    value = normalize_business_name("  Smith & Sons, LLC  ", "US")
    assert value.normalized_key == "smith and sons"
    assert value.version == "business_name_normalization_v1"
    assert "legal_suffix_removed" in value.transformations
    assert "and_equivalent" in value.transformations


@pytest.mark.parametrize("left,right,expected", [
    ("+1 202-555-0123", "+1 202-555-0123", "exact_match"),
    ("(202) 555-0123", "+1 202 555 0123", "semantic_match"),
    ("+1 202-555-0123 ext 8", "+1 202-555-0123", "semantic_match"),
    ("+1 202-555-0123 ext 8", "+1 202-555-0123 ext 9", "mismatch"),
    ("+1 202-555-0123", "+1 202-555-0199", "mismatch"),
])
def test_phone_comparison_uses_complete_number_and_separate_extension(left, right, expected):
    assert compare_phones(left, right, "US").state == expected


def test_invalid_phone_is_not_guessed_from_trailing_digits():
    value = normalize_phone("call 0123", "US")
    assert value.valid is False and value.normalized_key is None
    assert compare_phones("call 0123", "+1 202-555-0123", "US").state == "incomparable"


@pytest.mark.parametrize("left,right,expected", [
    ("123 Main St, Austin, TX 78701", "123 Main St, Austin, TX 78701", "exact_match"),
    ("123 Main Street, Austin, Texas 78701", "123 Main St, Austin, TX 78701", "semantic_match"),
    ({"streetAddress": "123 Main St", "addressLocality": "Austin", "addressRegion": "TX"},
     "123 Main Street, Austin, TX 78701", "semantic_match"),
    ("123 Main St Ste 8, Austin, TX", "123 Main St, Austin, TX", "semantic_match"),
    ("123 Main St Ste 8, Austin, TX", "123 Main St Ste 9, Austin, TX", "mismatch"),
    ("124 Main St, Austin, TX", "123 Main St, Austin, TX", "mismatch"),
    ("Austin, TX", "Austin, TX", "incomparable"),
])
def test_us_address_comparison_requires_matching_street_core(left, right, expected):
    assert compare_addresses(left, right, "US").state == expected


def test_non_us_unstructured_addresses_are_conservatively_incomparable():
    left = normalize_address("10 Downing Street, London", "GB")
    assert left.valid is False
    assert compare_addresses("10 Downing Street, London", "10 Downing St, London", "GB").state == "incomparable"


@pytest.mark.parametrize("site,gbp,expected", [
    (["Austin", "Round Rock"], ["Round Rock", "Austin"], "exact_match"),
    (["Austin, TX", "Round Rock, TX"], ["Austin", "Round Rock"], "exact_match"),
    (["Austin", "Round Rock"], ["Austin", "Pflugerville"], "partial_match"),
    (["Austin"], ["Dallas"], "mismatch"),
])
def test_service_area_comparison_uses_set_overlap(site, gbp, expected):
    result = compare_service_areas(site, gbp, "US", region="TX")
    assert result.state == expected


def test_service_area_does_not_infer_geographic_containment_or_split_marketing_text():
    result = compare_service_areas(["Greater Austin Area"], ["Austin"], "US", region="TX")
    assert result.state == "mismatch"
    assert result.site_only == ["greater austin area"]
