from __future__ import annotations

import hashlib
import gzip
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


class SavedFresnoProvider:
    """Replay captured responses keyed by the actual outgoing query, offline."""

    def __init__(self):
        self.queries = []

    async def search(self, query):
        self.queries.append(query)
        filenames = {
            "93722": "locations-fresno-93722.json",
            "Fresno, CA": "locations-fresno-ca.json",
            "Fresno, California": "locations-fresno-california.json",
        }
        assert query in filenames, f"Unexpected query: {query}"
        return FakeProvider(filenames[query]).response


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


def test_location_query_uses_one_provider_friendly_local_identifier() -> None:
    assert build_location_query(market()) == "Austin, Texas"
    assert build_location_query(market(postal_code="78701")) == "78701"
    assert build_location_query(market(
        display_name="Brooklyn, New York",
        city=None,
        region=None,
    )) == "Brooklyn, New York"


@pytest.mark.parametrize("region", ["CA", "ca", "California"])
def test_us_state_abbreviation_and_full_name_query_the_same_city(region):
    assert build_location_query(market(city="Fresno", region=region, display_name=f"Fresno, {region}")) == "Fresno, California"
    assert build_location_query(market(city=None, region=None, display_name="Fresno, CA")) == "Fresno, California"
    assert build_location_query(market(country_code="GB", city="Example", region="CA")) == "Example, CA"


@pytest.mark.anyio
@pytest.mark.parametrize("city", [None, "Fresno"], ids=["postal-only", "postal-and-city"])
async def test_saved_postal_response_does_not_require_city_text(city):
    provider = SavedFresnoProvider()
    point = await resolve_target_point(
        market(display_name="93722" if city is None else "Fresno, CA, US", city=city, region="CA", postal_code="93722"),
        provider=provider, clock=lambda: NOW,
    )
    assert provider.queries == ["93722"]
    assert point.canonical_name == "93722,California,United States"
    assert point.provider_location_id == "585069dbee19ad271e9c3fad"
    assert (point.latitude, point.longitude) == (36.8353312, -119.8930904)


@pytest.mark.anyio
@pytest.mark.parametrize("region", ["CA", "California"], ids=["Fresno-CA", "Fresno-California"])
async def test_saved_fresno_queries_choose_exact_city_not_dma_or_other_county_cities(region):
    provider = SavedFresnoProvider()
    point = await resolve_target_point(market(display_name=f"Fresno, {region}", city="Fresno", region=region),
        provider=provider, clock=lambda: NOW)
    assert provider.queries == ["Fresno, California"]
    assert point.canonical_name == "Fresno,California,United States"
    assert point.provider_location_id == "585069acee19ad271e9b78e0"
    bound = json.loads(gzip.decompress((FIXTURES.parent / "gold/all-star-plumbing-fresno/bound-p0-10.json.gz").read_bytes()))
    frozen_market = bound["request"]["evidence_input"]["context"]["target_market"]
    assert (point.latitude, point.longitude) == (frozen_market["latitude"], frozen_market["longitude"]) == (36.7468422, -119.7725868)


@pytest.mark.anyio
async def test_saved_dma_only_response_is_not_a_city_fallback():
    with pytest.raises(SerpLocationResolutionError, match="SERP_LOCATION_NOT_FOUND"):
        await resolve_target_point(market(display_name="Fresno, CA", city="Fresno", region="CA"),
            provider=FakeProvider("locations-fresno-ca.json"), clock=lambda: NOW)


@pytest.mark.anyio
async def test_nonexistent_city_cannot_match_saved_locations():
    with pytest.raises(SerpLocationResolutionError, match="SERP_LOCATION_NOT_FOUND"):
        await resolve_target_point(market(display_name="NoSuchCity, California", city="NoSuchCity", region="CA"),
            provider=FakeProvider("locations-fresno-california.json"), clock=lambda: NOW)


@pytest.mark.anyio
async def test_postal_city_exception_requires_exact_postal_code_and_postal_type():
    for postal, fixture in [("9372", "locations-fresno-93722.json"), ("93722", "locations-fresno-california.json")]:
        with pytest.raises(SerpLocationResolutionError, match="SERP_LOCATION_NOT_FOUND"):
            await resolve_target_point(market(city="Fresno", region="CA", postal_code=postal),
                provider=FakeProvider(fixture), clock=lambda: NOW)


@pytest.mark.anyio
@pytest.mark.parametrize("site", [
    "all-star-plumbing-fresno", "pr-plumbing-fresno", "vision-tech-swansea",
    "volcanoes-hiking-guides", "seamless-gutters-pensacola",
])
async def test_frozen_gold_coordinates_remain_explicit_and_never_lookup(site):
    path = FIXTURES.parent / "gold" / site / "bound-p0-10.json.gz"
    bound = json.loads(gzip.decompress(path.read_bytes()))
    raw = bound["request"]["evidence_input"]["context"]["target_market"]
    target = TargetMarket.model_validate(raw)
    assert target.latitude is not None and target.longitude is not None

    class NeverLookup:
        async def search(self, query):
            pytest.fail("Frozen explicit coordinates must not call the location provider")

    point = await resolve_target_point(target, provider=NeverLookup(), clock=lambda: NOW)
    assert point.source == "explicit_coordinates"
    assert (point.latitude, point.longitude) == (target.latitude, target.longitude)
    assert point.response_checksum is None


@pytest.mark.anyio
async def test_country_level_provider_result_is_not_a_local_search_point() -> None:
    class CountryProvider:
        async def search(self, query: str) -> LocationLookupResponse:
            return LocationLookupResponse(
                items=({
                    "id": "country-us",
                    "name": "United States",
                    "canonical_name": "United States",
                    "country_code": "US",
                    "target_type": "Country",
                    "gps": [-95.712891, 37.09024],
                },),
                response_checksum=f"sha256:{'d' * 64}",
            )

    with pytest.raises(SerpLocationResolutionError) as raised:
        await resolve_target_point(
            market(display_name="United States", city=None, region=None),
            provider=CountryProvider(),
            clock=lambda: NOW,
        )

    assert raised.value.code == "SERP_LOCATION_NOT_FOUND"


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
