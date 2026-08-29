from app.competitors_v22.normalization import (
    canonical_competitor_url,
    normalize_address,
    normalize_domain,
    normalize_name,
    tokenize_relevance_text,
)


def test_text_identity_normalization_is_stable() -> None:
    assert normalize_name("  ÁCME Plumbing, LLC  ") == "ácme plumbing"
    assert normalize_address("100 Main St.,  Austin, TX") == "100 main st austin tx"
    assert tokenize_relevance_text("24-Hour PLUMBERS near me") == {"24", "hour", "plumb"}


def test_domain_and_canonical_url_remove_non_identity_noise() -> None:
    raw = "http://WWW.Example.COM:80/services/?utm_source=ads#top"

    assert normalize_domain(raw) == "example.com"
    assert canonical_competitor_url(raw) == "https://example.com/"


def test_invalid_or_non_public_website_is_not_normalized() -> None:
    assert normalize_domain("mailto:hello@example.com") is None
    assert canonical_competitor_url("javascript:alert(1)") is None
