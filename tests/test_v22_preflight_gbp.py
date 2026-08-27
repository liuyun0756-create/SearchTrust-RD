import json
from pathlib import Path
from typing import Any

import pytest

from app.preflight_v22.extractors import extract_site_signals
import httpx

from app.preflight_v22.gbp import GoogleMapsUrlExpander, LimitedGbpLookup
from app.preflight_v22.urls import SafeUrl, UrlSafetyError


FIXTURES = Path(__file__).parent / "fixtures" / "v22_preflight"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def site_signals():
    return extract_site_signals((FIXTURES / "homepage_complete.html").read_text())


@pytest.mark.anyio
async def test_gbp_lookup_skips_provider_when_not_configured() -> None:
    calls: list[dict[str, str]] = []

    async def provider(params: dict[str, str]) -> dict[str, Any]:
        calls.append(params)
        return {}

    result = await LimitedGbpLookup(provider=provider, configured=False).lookup(
        site_url="https://example.com/",
        signals=site_signals(),
    )

    assert result.status == "unavailable"
    assert result.code == "GBP_LOOKUP_UNAVAILABLE"
    assert result.candidates == ()
    assert calls == []


@pytest.mark.anyio
async def test_gbp_lookup_executes_one_search_and_normalizes_candidates() -> None:
    payload = json.loads((FIXTURES / "gbp_candidates.json").read_text())
    calls: list[dict[str, str]] = []

    async def provider(params: dict[str, str]) -> dict[str, Any]:
        calls.append(params)
        return payload

    result = await LimitedGbpLookup(provider=provider, configured=True).lookup(
        site_url="https://example.com/",
        signals=site_signals(),
    )

    assert len(calls) == 1
    assert calls[0]["engine"] == "google_maps"
    assert calls[0]["type"] == "search"
    assert "example.com" in calls[0]["q"]
    assert result.status == "found"
    assert len(result.candidates) == 2
    assert result.candidates[0].business_name == "Acme Plumbing"
    assert result.candidates[0].market is not None
    assert result.candidates[0].market.city == "Austin"
    assert result.candidates[0].operating_model == "storefront"
    assert result.candidates[0].public_gbp_url.startswith("https://www.google.com/maps?cid=")
    assert not hasattr(result.candidates[0], "raw_response")


@pytest.mark.anyio
async def test_gbp_lookup_uses_exact_place_id_without_second_request() -> None:
    calls: list[dict[str, str]] = []

    async def provider(params: dict[str, str]) -> dict[str, Any]:
        calls.append(params)
        return {"place_results": {"title": "Acme Plumbing", "website": "https://example.com"}}

    result = await LimitedGbpLookup(provider=provider, configured=True).lookup(
        site_url="https://example.com/",
        signals=site_signals(),
        gbp_url=(
            "https://www.google.com/maps/dir/?api=1&"
            "destination_place_id=ChIJ123456789"
        ),
    )

    assert calls == [{"engine": "google_maps", "hl": "en", "place_id": "ChIJ123456789"}]
    assert result.status == "found"


@pytest.mark.anyio
async def test_gbp_lookup_expands_short_url_before_single_provider_request() -> None:
    provider_calls: list[dict[str, str]] = []
    expander_calls: list[str] = []

    async def provider(params: dict[str, str]) -> dict[str, Any]:
        provider_calls.append(params)
        return {"place_results": {"title": "Acme Plumbing", "website": "https://example.com"}}

    async def expander(value: str) -> str:
        expander_calls.append(value)
        return (
            "https://www.google.com/maps/place/Acme/"
            "data=!4m2!3m1!1s0x8644b5a123456789:0x1234567890abcdef"
        )

    result = await LimitedGbpLookup(
        provider=provider,
        configured=True,
        url_expander=expander,
    ).lookup(
        site_url="https://example.com/",
        signals=site_signals(),
        gbp_url="https://maps.app.goo.gl/abc123",
    )

    assert expander_calls == ["https://maps.app.goo.gl/abc123"]
    assert provider_calls == [{
        "engine": "google_maps",
        "hl": "en",
        "data_cid": "1311768467294899695",
    }]
    assert result.status == "found"


@pytest.mark.anyio
async def test_short_url_expander_rejects_unsafe_redirect_before_request() -> None:
    requested: list[str] = []

    async def resolver(_: str) -> list[str]:
        return ["8.8.8.8"]

    def client_factory(target: SafeUrl) -> httpx.AsyncClient:
        def handler(request: httpx.Request) -> httpx.Response:
            requested.append(str(request.url))
            return httpx.Response(302, headers={"location": "http://127.0.0.1/admin"})

        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    expander = GoogleMapsUrlExpander(
        resolver=resolver,
        client_factory=client_factory,
    )

    with pytest.raises(UrlSafetyError) as exc_info:
        await expander.expand("https://maps.app.goo.gl/abc123")

    assert exc_info.value.code == "URL_ADDRESS_FORBIDDEN"
    assert requested == ["https://maps.app.goo.gl/abc123"]


@pytest.mark.anyio
async def test_gbp_lookup_degrades_provider_error_without_leaking_detail() -> None:
    async def provider(_: dict[str, str]) -> dict[str, Any]:
        raise RuntimeError("secret provider response")

    result = await LimitedGbpLookup(provider=provider, configured=True).lookup(
        site_url="https://example.com/",
        signals=site_signals(),
    )

    assert result.status == "unavailable"
    assert result.code == "GBP_LOOKUP_UNAVAILABLE"
    assert "secret" not in result.message


@pytest.mark.anyio
async def test_gbp_lookup_ignores_malformed_provider_candidate() -> None:
    async def provider(_: dict[str, str]) -> dict[str, Any]:
        return {
            "local_results": [{
                "title": "x" * 241,
                "website": "https://example.com/",
                "gps_coordinates": {"latitude": 999, "longitude": 999},
            }],
        }

    result = await LimitedGbpLookup(provider=provider, configured=True).lookup(
        site_url="https://example.com/",
        signals=site_signals(),
    )

    assert result.status == "not_found"
    assert result.candidates == ()
