import hashlib
import json
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import SecretStr

from app.api.v2.models import PreflightRequest
from app.core.config import settings
from app.preflight_v22 import service as preflight_service
from app.preflight_v22.fetcher import HomepageFetchError, HomepageSnapshot
from app.preflight_v22.gbp import GbpCandidate, GbpLookupResult, LimitedGbpLookup
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
@pytest.mark.parametrize("scenario", ["address_only", "place_id", "empty_search", "real_conflict", "explicit_link"])
async def test_site_map_fallback_is_bounded_and_keeps_branch_binding(scenario, monkeypatch):
    monkeypatch.setattr(settings, "V22_INTERNAL_API_TOKEN", SecretStr("local-test-key"))
    calls = []
    site = "https://seamlessgutterspensacola.com/"
    short_link = "https://maps.app.goo.gl/8MMJyNe2na4og54U7"
    gold_cid = "16921898606511158364"
    old_cid = "10354290509108082871"
    address = "10025 Ashton Brosnaham Dr, Pensacola, FL 32534"
    html = ('<script type="application/ld+json">'
            '{"@type":"Organization","name":"Seamless Gutters of Pensacola","telephone":"+18507761782"}'
            '</script>'
            f'<a href="{short_link}">Get directions</a>')
    address_place = {"title": "10025 Ashton Brosnaham Dr", "address": address,
                     "type": "Building", "data_id": "0x8890945269cf386b:0x8fb1d45bd095f8b7"}
    gold_place = {"title": "Gutter Solutions & Home Improvements", "address": address,
                  "website": site, "phone": "(850) 776-1782", "type": "Gutter service",
                  "data_id": "0x8890edd63f678bf3:0xead6a9206646745c"}
    wrong_business = {"title": "Another gutter company", "address": address,
                      "website": "https://example.org/", "phone": "(850) 555-0101",
                      "type": "Gutter service", "data_id": gold_place["data_id"]}

    async def expand(gbp_url):
        assert gbp_url == short_link
        key, value = ("place_id", "address-place-id") if scenario == "place_id" else ("cid", old_cid)
        return f"https://www.google.com/maps?{key}={value}"

    async def provider(params):
        calls.append(params)
        if params["type"] == "place":
            return {"place_results": wrong_business if scenario == "real_conflict" else address_place}
        assert "seamlessgutterspensacola.com" in params["q"]
        assert "Seamless Gutters of Pensacola" in params["q"]
        return {"local_results": []} if scenario == "empty_search" else {"place_results": gold_place}

    cache = MemoryCache()
    service = PreflightService(
        fetcher=FakeFetcher(HomepageSnapshot(site, site, 200, html)),
        gbp_lookup=LimitedGbpLookup(provider=provider, configured=True, url_expander=expand),
        cache=cache, serpapi_configured=True, pagespeed_configured=False,
    )
    req = PreflightRequest(site_url=site, gbp_url=short_link if scenario == "explicit_link" else None)
    response = await service.run(req)
    if scenario in {"real_conflict", "explicit_link"}:
        assert [call["type"] for call in calls] == ["place"]
        assert response.branch_options is None
        assert "gbp_lookup_reason" not in response.model_dump(mode="json")
        fields = response.identity_candidates[0].field_comparisons
        assert fields[0].status == "not_matched"
        return

    assert [call["type"] for call in calls] == ["place", "search"]
    assert calls[0].get("data_cid") == (None if scenario == "place_id" else old_cid)
    assert response.gbp_lookup_reason == "site_map_link_address_only"
    assert cache.set_value.gbp_lookup_reason == "site_map_link_address_only"
    if scenario == "empty_search":
        assert response.branch_options is None
        assert any(gap.gap_code == "GBP_NOT_FOUND" for gap in response.data_gaps)
        return

    (branch,) = response.branch_options
    assert (branch.ref, branch.name, branch.domain_matched, branch.phone_matched) == (
        gold_cid, "Gutter Solutions & Home Improvements", True, True,
    )


@pytest.mark.anyio
async def test_no_site_map_link_preserves_main_preflight_bytes(monkeypatch):
    monkeypatch.setattr(preflight_service, "uuid4", lambda: UUID("0b9b1c33-cc35-49f0-a337-748d558db737"))
    monkeypatch.setattr(settings, "V22_INTERNAL_API_TOKEN", SecretStr(""))
    monkeypatch.setattr(settings, "V22_TARGET_KEYWORDS_ENABLED", False)
    site = "https://example.com/"
    calls = []

    async def provider(params):
        calls.append(params)
        return {"place_results": {
            "title": "100 Main St", "address": "100 Main St, Austin, TX 78701",
            "type": "Building", "data_id": "0x1:0x2",
        }}

    response = await PreflightService(
        fetcher=FakeFetcher(HomepageSnapshot(site, site, 200, "<title>Acme Plumbing</title>")),
        gbp_lookup=LimitedGbpLookup(provider=provider, configured=True),
        cache=MemoryCache(), serpapi_configured=True, pagespeed_configured=False,
    ).run(PreflightRequest(site_url=site))

    assert [call["type"] for call in calls] == ["search"]
    # Full response bytes from main c32778c with the same fixed preflight ID.
    assert hashlib.sha256(response.model_dump_json().encode()).hexdigest() == (
        "52428f80eb147a2ab3f957a5a428b9687cd59f628fd361650bd413f8688a8fea"
    )


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
