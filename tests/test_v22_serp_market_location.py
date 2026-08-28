from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
import httpx

from app.collectors.serp_market_location import (
    LocationLookupResponse,
    SerpApiLocationProvider,
    SerpLocationResolutionError,
    build_location_query,
    resolve_target_point,
)
from app.report_v22.models import TargetMarket


FIXTURES = Path(__file__).parent / "fixtures" / "v22_serp_market"
NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class FakeProvider:
    def __init__(self, fixture: str) -> None:
        raw = (FIXTURES / fixture).read_bytes()
        self.response = LocationLookupResponse(
            items=tuple(json.loads(raw)),
            response_checksum=f"sha256:{hashlib.sha256(raw).hexdigest()}",
        )
        self.queries: list[str] = []

    async def search(self, query: str) -> LocationLookupResponse:
        self.queries.append(query)
        return self.response


def market(**overrides: object) -> TargetMarket:
    values = {
        "display_name": "Austin, TX, US",
        "country_code": "US",
        "region": "TX",
        "city": "Austin",
        "postal_code": None,
        "latitude": None,
        "longitude": None,
    }
    values.update(overrides)
    return TargetMarket.model_validate(values)


@pytest.mark.anyio
async def test_explicit_coordinates_skip_location_provider() -> None:
    point = await resolve_target_point(
        market(latitude=30.2672, longitude=-97.7431),
        provider=None,
        clock=lambda: NOW,
    )

    assert point.source == "explicit_coordinates"
    assert point.latitude == 30.2672
    assert point.response_checksum is None


@pytest.mark.anyio
async def test_incomplete_explicit_coordinates_are_rejected() -> None:
    with pytest.raises(SerpLocationResolutionError) as raised:
        await resolve_target_point(
            market(latitude=30.2672),
            provider=None,
            clock=lambda: NOW,
        )

    assert raised.value.code == "SERP_LOCATION_COORDINATES_INCOMPLETE"


@pytest.mark.anyio
async def test_location_resolution_prefers_city_and_filters_country() -> None:
    provider = FakeProvider("locations-austin.json")

    point = await resolve_target_point(
        market(),
        provider=provider,
        clock=lambda: NOW,
    )

    assert len(provider.queries) == 1
    assert provider.queries[0] == build_location_query(market())
    assert point.source == "serpapi_location"
    assert point.provider_location_id == "city-austin"
    assert point.canonical_name == "Austin,Texas,United States"
    assert point.latitude == 30.267153
    assert point.longitude == -97.7430608


@pytest.mark.anyio
async def test_location_resolution_rejects_distinct_tied_points() -> None:
    provider = FakeProvider("locations-ambiguous.json")

    with pytest.raises(SerpLocationResolutionError) as raised:
        await resolve_target_point(
            market(display_name="Springfield, US", city="Springfield", region=None),
            provider=provider,
            clock=lambda: NOW,
        )

    assert raised.value.code == "SERP_LOCATION_AMBIGUOUS"


@pytest.mark.anyio
async def test_location_resolution_rejects_country_mismatch() -> None:
    provider = FakeProvider("locations-austin.json")

    with pytest.raises(SerpLocationResolutionError) as raised:
        await resolve_target_point(
            market(country_code="GB"),
            provider=provider,
            clock=lambda: NOW,
        )

    assert raised.value.code == "SERP_LOCATION_NOT_FOUND"


@pytest.mark.anyio
async def test_location_provider_bounds_response_and_uses_one_free_query() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=b"[" + b" " * 128 + b"]")

    provider = SerpApiLocationProvider(
        url="https://serpapi.example/locations.json",
        connect_timeout=1,
        read_timeout=1,
        total_timeout=2,
        max_response_bytes=64,
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(SerpLocationResolutionError) as raised:
        await provider.search("Austin, US")

    assert raised.value.code == "SERP_LOCATION_RESPONSE_TOO_LARGE"
    assert len(requests) == 1
    assert requests[0].url.params["q"] == "Austin, US"
    assert requests[0].url.params["limit"] == "10"
