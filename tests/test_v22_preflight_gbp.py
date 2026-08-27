import json
from pathlib import Path
from typing import Any

import pytest

from app.preflight_v22.extractors import extract_site_signals
from app.preflight_v22.gbp import LimitedGbpLookup


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
