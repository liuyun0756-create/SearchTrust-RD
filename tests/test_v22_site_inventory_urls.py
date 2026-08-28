import pytest

from app.collectors.site_inventory_urls import (
    SiteScope,
    canonicalize_inventory_url,
    stable_url_digest,
)


def test_canonicalizes_www_default_document_and_query_order() -> None:
    scope = SiteScope.from_root("https://example.com/")

    result = canonicalize_inventory_url(
        "HTTPS://WWW.Example.com:443/services/index.html/?b=2&utm_source=ad&a=1#details",
        scope=scope,
    )

    assert result == "https://example.com/services?a=1&b=2"


def test_resolves_relative_urls_and_removes_trailing_slash() -> None:
    scope = SiteScope.from_root("https://example.com/")

    result = canonicalize_inventory_url(
        "../areas/austin/",
        base_url="https://example.com/services/plumbing/",
        scope=scope,
    )

    assert result == "https://example.com/services/areas/austin"


@pytest.mark.parametrize(
    "url",
    [
        "https://shop.example.com/service",
        "https://other.example/service",
        "mailto:hello@example.com",
        "https://example.com/login",
        "https://example.com/cart",
        "https://example.com/search?q=plumber",
        "https://example.com/services?sort=price",
        "https://example.com/assets/logo.png",
        "https://example.com/wp-admin/delete-user",
    ],
)
def test_rejects_out_of_scope_or_low_value_urls(url: str) -> None:
    scope = SiteScope.from_root("https://example.com/")

    assert canonicalize_inventory_url(url, scope=scope) is None


def test_scope_can_preserve_confirmed_www_host() -> None:
    scope = SiteScope.from_root("https://www.example.com/")

    assert canonicalize_inventory_url("https://example.com/about", scope=scope) == (
        "https://www.example.com/about"
    )


def test_stable_digest_never_contains_raw_url() -> None:
    url = "https://example.com/private?customer=alice"

    digest = stable_url_digest(url)

    assert digest.startswith("sha256:")
    assert len(digest) == 71
    assert "alice" not in digest
