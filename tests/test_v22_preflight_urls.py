import socket

import pytest

from app.preflight_v22.urls import (
    UrlSafetyError,
    UrlUnreachableError,
    normalize_site_url,
    resolve_public_url,
    validate_gbp_url,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_normalize_site_url_uses_safe_root_identity() -> None:
    assert normalize_site_url("HTTPS://WWW.Example.COM:443/a/b?q=1#section") == (
        "https://www.example.com/"
    )
    assert normalize_site_url("http://Example.com:80/path") == "http://example.com/"


def test_normalize_site_url_supports_idna_and_ipv6() -> None:
    assert normalize_site_url("https://münich.example/path") == (
        "https://xn--mnich-kva.example/"
    )
    assert normalize_site_url("https://[2606:4700:4700::1111]/dns-query") == (
        "https://[2606:4700:4700::1111]/"
    )


@pytest.mark.parametrize(
    ("url", "code"),
    [
        ("ftp://example.com/file", "URL_SCHEME_UNSUPPORTED"),
        ("https://user:secret@example.com", "URL_USERINFO_FORBIDDEN"),
        ("https://example.com:8080", "URL_PORT_FORBIDDEN"),
        ("https://localhost", "URL_HOST_FORBIDDEN"),
        ("http://metadata.google.internal/latest", "URL_HOST_FORBIDDEN"),
        ("http://127.0.0.1/admin", "URL_ADDRESS_FORBIDDEN"),
        ("http://169.254.169.254/latest", "URL_ADDRESS_FORBIDDEN"),
        ("http://[::1]/", "URL_ADDRESS_FORBIDDEN"),
    ],
)
def test_normalize_site_url_rejects_unsafe_syntax_and_direct_targets(
    url: str,
    code: str,
) -> None:
    with pytest.raises(UrlSafetyError) as exc_info:
        normalize_site_url(url)

    assert exc_info.value.code == code


@pytest.mark.anyio
async def test_resolve_public_url_rejects_domain_if_any_address_is_not_global() -> None:
    async def resolver(hostname: str) -> list[str]:
        assert hostname == "example.com"
        return ["93.184.216.34", "10.0.0.8"]

    with pytest.raises(UrlSafetyError) as exc_info:
        await resolve_public_url("https://example.com/page", resolver=resolver)

    assert exc_info.value.code == "URL_ADDRESS_FORBIDDEN"


@pytest.mark.anyio
async def test_resolve_public_url_returns_stable_unique_public_addresses() -> None:
    async def resolver(_: str) -> list[str]:
        return ["2606:4700:4700::1111", "8.8.8.8", "8.8.8.8"]

    target = await resolve_public_url("https://example.com/page", resolver=resolver)

    assert target.normalized_url == "https://example.com/"
    assert target.hostname == "example.com"
    assert target.addresses == ("2606:4700:4700::1111", "8.8.8.8")


@pytest.mark.anyio
async def test_resolve_public_url_classifies_dns_failure_as_unreachable() -> None:
    async def resolver(_: str) -> list[str]:
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")

    with pytest.raises(UrlUnreachableError) as exc_info:
        await resolve_public_url("https://missing.example", resolver=resolver)

    assert exc_info.value.code == "SITE_DNS_UNAVAILABLE"


@pytest.mark.parametrize(
    "url",
    [
        "https://www.google.com/maps/place/Example/data=!4m2!3m1!1s0x1:0x2",
        "https://maps.google.com/?cid=123",
        "https://search.google.com/local/reviews?placeid=abc",
        "https://maps.app.goo.gl/abc123",
        "https://goo.gl/maps/abc123",
        "https://share.google/abc123",
    ],
)
def test_validate_gbp_url_accepts_supported_google_maps_urls(url: str) -> None:
    assert validate_gbp_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/maps/place/fake",
        "https://evil.google.example/maps/place/fake",
        "https://goo.gl/not-maps",
        "https://www.google.com/search?q=business",
        "http://127.0.0.1/maps",
    ],
)
def test_validate_gbp_url_rejects_non_gbp_targets(url: str) -> None:
    with pytest.raises(UrlSafetyError) as exc_info:
        validate_gbp_url(url)

    assert exc_info.value.code in {"GBP_URL_UNSUPPORTED", "URL_ADDRESS_FORBIDDEN"}
