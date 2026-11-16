"""Saved smoke-test shapes; no private fixtures or provider calls required."""
import json
from pathlib import Path
from uuid import UUID

import pytest

from app.api.v2.models import ConfirmedCompetitor
from app.collectors.site_inventory import SiteInventoryCollector
from app.collectors.site_inventory_fetcher import SiteFetchError
from app.collectors.site_inventory_html import HtmlStructure
from app.collectors.site_inventory_models import InventoryPageRecordV2
from app.collectors.site_inventory_selection import classify_page
from app.competitors_v22.site_stage import CheckpointedCompetitorSiteStage
from app.report_v22.models import TargetMarket
from test_v22_site_inventory_collector import FakeFetcher, FakeFirecrawl, NOW, html_response


FEWER_PAGES = "The website exposed fewer pages than the sampling target."
ROOT = "https://www.artdouglasplumbing.com/"
SERVICE = ROOT + "plumbing-services"
FIXTURE = json.loads((Path(__file__).parent / "fixtures/site_inventory/pr-service-classification-f4a76990.json").read_text())


@pytest.fixture
def anyio_backend():
    return "asyncio"


def make_collector(fetcher, *, resource_limits=True, contact_coverage=False, structural_max_bytes=256_000):
    return SiteInventoryCollector(
        fetcher=fetcher, firecrawl=FakeFirecrawl(), structural_max_bytes=structural_max_bytes,
        sitemap_max_bytes=100_000, sitemap_decompressed_max_bytes=500_000,
        sitemap_max_files=5, sitemap_max_depth=2, batch_size=2,
        resource_limits=resource_limits, sampling_quality=True,
        contact_coverage=contact_coverage, clock=lambda: NOW,
    )


def art_pages():
    return {
        ROOT: html_response(ROOT, '<h1>Home</h1><a href="/plumbing-services">Services</a><a href="/madera-county">Area</a>'),
        SERVICE: html_response(SERVICE, '<title>Plumbing Services</title><h1>Plumbing Services</h1>'),
        ROOT + "madera-county": html_response(
            ROOT + "madera-county",
            '<link rel="canonical" href="/dmPageNotFound"><h1>Page not found</h1>', status=404,
        ),
    }


@pytest.mark.anyio
@pytest.mark.parametrize(("resource_limits", "contact_coverage"), [(False, False), (True, False), (True, True)], ids=["legacy", "competitor-v2", "customer-v3"])
async def test_art_404_canonical_preserves_existing_record_type_and_home_service(resource_limits, contact_coverage):
    inventory = await make_collector(FakeFetcher(art_pages()), resource_limits=resource_limits, contact_coverage=contact_coverage).collect(
        site_url=ROOT, discovery_limit=50, deep_analysis_limit=10,
    )
    assert {str(p.url) for p in inventory.selected_pages if p.deep_analyzed} == {ROOT, SERVICE}
    error = next(p for p in inventory.pages if p.status_code == 404)
    assert str(error.url) == ROOT + "dmPageNotFound"
    assert isinstance(error, InventoryPageRecordV2) == resource_limits
    if resource_limits:
        assert error.structural_truncated is False
    assert FEWER_PAGES not in inventory.limitations
    assert "page_failures_present" in inventory.limitations


class ActualCollectorAdapter:
    def __init__(self, collector):
        self.collector = collector

    async def collect_inventory_for_site(self, **kwargs):
        return await self.collector.collect(**{k:kwargs[k] for k in ["site_url", "primary_service", "target_market", "discovery_limit", "deep_analysis_limit", "gsc_priorities"]})


@pytest.mark.anyio
@pytest.mark.parametrize("small_site", [False, True], ids=["art-canonical-404", "low-cost-single-page"])
async def test_real_competitor_stage_retains_usable_inventory(small_site):
    pages = {ROOT: html_response(ROOT, '<h1>Home</h1>')} if small_site else art_pages()
    result, = await CheckpointedCompetitorSiteStage(ActualCollectorAdapter(make_collector(FakeFetcher(pages)))).collect(
        job_id=UUID("f4a76990-ee4d-4c1f-ab1f-e83cb85b51ee"),
        competitors=[ConfirmedCompetitor(competitor_id="cp_art", business_name="Art Douglas", website_url=ROOT,
                                       public_gbp_url="https://www.google.com/maps?cid=123", confirmation_source="user")],
        primary_service="Plumber", target_market=TargetMarket(country_code="US", region="California", city="Fresno", display_name="Fresno, California, US"),
        max_pages_each=10, checkpoints=None,
    )
    assert result.status == "partial"
    assert result.inventory is not None
    assert result.inventory.discovery_limit == 50
    assert result.inventory.deep_analysis_limit == 10
    assert result.inventory.deep_analyzed_count == (1 if small_site else 2)
    assert (FEWER_PAGES in result.limitations) == small_site
    assert ("candidate_exhausted" in result.limitations) == (not small_site)


@pytest.mark.anyio
async def test_failed_structural_request_is_not_normal_small_site_completion():
    pages = art_pages()
    pages[ROOT + "madera-county"] = SiteFetchError("page_timeout", "request failed")
    result = await make_collector(FakeFetcher(pages)).collect(site_url=ROOT, discovery_limit=50, deep_analysis_limit=10)
    assert FEWER_PAGES not in result.limitations
    assert "candidate_exhausted" in result.limitations
    assert "page_failures_present" in result.limitations


@pytest.mark.anyio
async def test_failed_deep_request_is_not_normal_small_site_completion():
    pages = {
        ROOT: [html_response(ROOT, '<a href="/plumbing-services">Service</a>'), html_response(ROOT, '<h1>Home</h1>')],
        SERVICE: [html_response(SERVICE, '<h1>Plumbing Services</h1>'), SiteFetchError("page_timeout", "deep failed")],
    }
    result = await make_collector(FakeFetcher(pages), structural_max_bytes=1).collect(site_url=ROOT, discovery_limit=2, deep_analysis_limit=10)
    assert FEWER_PAGES not in result.limitations
    assert "deep_page_failures_present" in result.limitations
    assert "candidate_exhausted" in result.limitations


@pytest.mark.parametrize("page", FIXTURE["pages"], ids=lambda p: p["url"].split(".com/", 1)[1])
def test_saved_pr_url_title_h1_classification(page):
    result = classify_page(url=page["url"], root_url=FIXTURE["root_url"], structure=HtmlStructure(
        title=page["title"], h1=page["h1"], schema_types=tuple(page["schema_types"]),
        canonical_url=None, meta_robots=(), internal_links=(), visible_text="",
    ))
    assert result.page_type == page["expected"]


@pytest.mark.parametrize("path", ["/blog/plumbing-repairs", "/how-to-fix-plumbing-repairs", "/plumbing-repair-tips", "/careers", "/specials", "/news/commercial-plumbing", "/plumbing-repairs-discounts"])
def test_service_words_do_not_promote_editorial_or_non_service_pages(path):
    result = classify_page(url="https://example.com"+path, root_url="https://example.com/", structure=HtmlStructure(
        title="Plumbing repairs and services", h1="Plumbing repairs and services",
        schema_types=("ProfessionalService",), canonical_url=None, meta_robots=(), internal_links=(), visible_text="",
    ))
    assert result.page_type not in {"service_index", "service_detail"}
