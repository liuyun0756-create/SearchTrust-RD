import json
from pathlib import Path

import pytest

from app.api.v2.models import PreflightRequest
from app.preflight_v22.fetcher import HomepageFetchError, HomepageSnapshot
from app.preflight_v22.gbp import GbpCandidate, GbpLookupResult
from app.preflight_v22.service import PreflightService
from app.report_v22.models import TargetMarket


FIXTURE = Path(__file__).parent / "fixtures" / "v22_preflight" / "homepage_complete.html"
MODULE_KEYS = [
    "site_inventory",
    "site_deep_analysis",
    "serp_maps",
    "serp_local_pack",
    "serp_organic",
    "public_gbp",
    "competitor_analysis",
    "pagespeed",
]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def request_payload(*, with_context: bool = True) -> PreflightRequest:
    payload = {"site_url": "https://example.com"}
    if with_context:
        payload.update({
            "primary_service": "Emergency Plumbing",
            "target_market": {
                "display_name": "Austin, TX, US",
                "country_code": "US",
                "region": "TX",
                "city": "Austin",
            },
        })
    return PreflightRequest.model_validate_json(json.dumps(payload))


class FakeFetcher:
    def __init__(self, result) -> None:
        self.result = result
        self.calls = 0

    async def fetch(self, _: str):
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeGbpLookup:
    def __init__(self, result: GbpLookupResult, *, configured: bool = True) -> None:
        self.result = result
        self.configured = configured
        self.calls = 0

    async def lookup(self, **_):
        self.calls += 1
        return self.result


class MemoryCache:
    def __init__(self, value=None) -> None:
        self.value = value
        self.set_value = None

    def key(self, *_):
        return "cache-key"

    async def get(self, _):
        return self.value

    async def set(self, _, value):
        self.set_value = value


def snapshot() -> HomepageSnapshot:
    return HomepageSnapshot(
        source_url="https://example.com/",
        normalized_site_url="https://example.com/",
        status_code=200,
        html=FIXTURE.read_text(),
    )


def gbp_found() -> GbpLookupResult:
    candidate = GbpCandidate(
        business_name="Acme Plumbing",
        website_url="https://example.com/",
        public_gbp_url="https://www.google.com/maps?cid=1311768467294899695",
        phone="+1 512-555-0100",
        address="100 Congress Ave, Austin, TX 78701",
        market=TargetMarket(
            display_name="Austin, TX, US",
            country_code="US",
            region="TX",
            city="Austin",
            postal_code="78701",
        ),
        operating_model="storefront",
        categories=("Plumber",),
    )
    return GbpLookupResult("found", "GBP_FOUND", "Public GBP candidates were found.", (candidate,))


@pytest.mark.anyio
async def test_service_builds_complete_frozen_response_and_all_modules() -> None:
    cache = MemoryCache()
    service = PreflightService(
        fetcher=FakeFetcher(snapshot()),
        gbp_lookup=FakeGbpLookup(gbp_found()),
        cache=cache,
        serpapi_configured=True,
        pagespeed_configured=True,
    )

    result = await service.run(request_payload())

    assert [item.module_key for item in result.module_availability] == MODULE_KEYS
    assert all(item.available for item in result.module_availability)
    assert result.competitor_candidates == []
    assert result.estimated_duration_bucket == "10_to_15_minutes"
    assert result.identity_candidates[0].business.business_name == "Acme Plumbing"
    pending = next(gap for gap in result.data_gaps if gap.gap_code == "COMPETITOR_DISCOVERY_PENDING")
    assert pending.blocking is False
    assert cache.set_value == result
    serialized = result.model_dump(mode="json")
    assert "findings" not in serialized
    assert "top_actions" not in serialized
    assert "provider_cost" not in serialized


@pytest.mark.anyio
async def test_service_returns_degraded_response_when_site_is_unreachable() -> None:
    service = PreflightService(
        fetcher=FakeFetcher(HomepageFetchError("SITE_UNREACHABLE", "The site is unavailable.")),
        gbp_lookup=FakeGbpLookup(
            GbpLookupResult("unavailable", "GBP_LOOKUP_UNAVAILABLE", "GBP unavailable.", ()),
            configured=False,
        ),
        cache=MemoryCache(),
        serpapi_configured=False,
        pagespeed_configured=False,
    )

    result = await service.run(request_payload(with_context=False))

    gaps = {gap.gap_code: gap for gap in result.data_gaps}
    assert gaps["SITE_UNREACHABLE"].blocking is True
    assert gaps["BUSINESS_IDENTITY_UNCONFIRMED"].blocking is True
    assert gaps["PRIMARY_SERVICE_MISSING"].blocking is True
    assert gaps["TARGET_MARKET_MISSING"].blocking is True
    assert result.estimated_duration_bucket == "under_5_minutes"
    assert all(not item.available for item in result.module_availability)


@pytest.mark.anyio
async def test_service_cache_hit_skips_fetch_and_provider() -> None:
    initial_fetcher = FakeFetcher(snapshot())
    initial_lookup = FakeGbpLookup(gbp_found())
    first_service = PreflightService(
        fetcher=initial_fetcher,
        gbp_lookup=initial_lookup,
        cache=MemoryCache(),
        serpapi_configured=True,
        pagespeed_configured=True,
    )
    cached = await first_service.run(request_payload())
    fetcher = FakeFetcher(AssertionError("fetch should not run"))
    lookup = FakeGbpLookup(gbp_found())
    service = PreflightService(
        fetcher=fetcher,
        gbp_lookup=lookup,
        cache=MemoryCache(cached),
        serpapi_configured=True,
        pagespeed_configured=True,
    )

    result = await service.run(request_payload())

    assert result.preflight_id == cached.preflight_id
    assert fetcher.calls == 0
    assert lookup.calls == 0


@pytest.mark.anyio
async def test_service_marks_provider_modules_unavailable_without_failing_site() -> None:
    service = PreflightService(
        fetcher=FakeFetcher(snapshot()),
        gbp_lookup=FakeGbpLookup(
            GbpLookupResult("unavailable", "GBP_LOOKUP_UNAVAILABLE", "GBP unavailable.", ()),
            configured=False,
        ),
        cache=MemoryCache(),
        serpapi_configured=False,
        pagespeed_configured=False,
    )

    result = await service.run(request_payload())

    availability = {item.module_key: item.available for item in result.module_availability}
    assert availability["site_inventory"] is True
    assert availability["site_deep_analysis"] is True
    assert availability["public_gbp"] is False
    assert availability["serp_maps"] is False
    assert availability["competitor_analysis"] is False
    assert availability["pagespeed"] is False
    assert "SERP_PROVIDER_UNAVAILABLE" in {gap.gap_code for gap in result.data_gaps}
