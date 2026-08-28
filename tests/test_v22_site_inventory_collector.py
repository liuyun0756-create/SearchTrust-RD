from datetime import datetime, timezone

import pytest

from app.collectors.site_inventory import SiteInventoryCollectionError, SiteInventoryCollector
from app.collectors.site_inventory_fetcher import SiteFetchError, SiteFetchResponse
from app.collectors.site_inventory_firecrawl import FirecrawlMapResult


NOW = datetime(2026, 8, 28, 8, 0, tzinfo=timezone.utc)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def html_response(url: str, html: str, *, status: int = 200) -> SiteFetchResponse:
    return SiteFetchResponse(
        requested_url=url,
        final_url=url,
        status_code=status,
        content_type="text/html; charset=utf-8",
        body=html.encode(),
    )


class FakeFetcher:
    def __init__(
        self,
        pages: dict[str, SiteFetchResponse | Exception | list[SiteFetchResponse | Exception]],
    ) -> None:
        self.pages = pages
        self.calls: list[str] = []
        self.crawl_delays: list[float] = []

    async def fetch(self, url: str, **_: object) -> SiteFetchResponse:
        self.calls.append(url)
        value = self.pages.get(url)
        if value is None:
            return SiteFetchResponse(
                requested_url=url,
                final_url=url,
                status_code=404,
                content_type="text/plain",
                body=b"missing",
            )
        if isinstance(value, list):
            value = value.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    def apply_crawl_delay(self, seconds: float | None) -> None:
        if seconds is not None:
            self.crawl_delays.append(seconds)


class FakeFirecrawl:
    def __init__(self, urls: tuple[str, ...] = ()) -> None:
        self.urls = urls
        self.calls = 0

    async def map(self, root_url: str, *, limit: int) -> FirecrawlMapResult:
        self.calls += 1
        return FirecrawlMapResult(self.urls[:limit], None)


def collector(fetcher: FakeFetcher, firecrawl: FakeFirecrawl | None = None) -> SiteInventoryCollector:
    return SiteInventoryCollector(
        fetcher=fetcher,
        firecrawl=firecrawl,
        structural_max_bytes=256_000,
        sitemap_max_bytes=100_000,
        sitemap_decompressed_max_bytes=500_000,
        sitemap_max_files=5,
        sitemap_max_depth=2,
        batch_size=2,
        clock=lambda: NOW,
    )


@pytest.mark.anyio
async def test_collects_without_sitemap_through_internal_links() -> None:
    pages = {
        "https://example.com/": html_response(
            "https://example.com/",
            '<h1>Home</h1><a href="/services/plumbing">Plumbing</a><a href="/about">About</a>',
        ),
        "https://example.com/services/plumbing": html_response(
            "https://example.com/services/plumbing",
            "<title>Plumbing</title><h1>Plumbing</h1>",
        ),
        "https://example.com/about": html_response(
            "https://example.com/about",
            "<title>About</title><h1>About</h1>",
        ),
    }
    fetcher = FakeFetcher(pages)
    firecrawl = FakeFirecrawl(("https://example.com/extra",))

    result = await collector(fetcher, firecrawl).collect(
        site_url="https://example.com/",
        discovery_limit=3,
        deep_analysis_limit=2,
    )

    assert [str(page.url) for page in result.pages] == [
        "https://example.com/",
        "https://example.com/services/plumbing",
        "https://example.com/about",
    ]
    assert result.discovered_url_count == 3
    assert result.structurally_checked_count == 3
    assert firecrawl.calls == 0


@pytest.mark.anyio
async def test_deduplicates_tracking_www_and_canonical_urls() -> None:
    pages = {
        "https://example.com/": html_response(
            "https://example.com/",
            '<a href="/about?utm_source=x">About</a><a href="https://www.example.com/about/">Again</a>',
        ),
        "https://example.com/about": html_response(
            "https://example.com/about",
            '<link rel="canonical" href="https://www.example.com/about/"><h1>About</h1>',
        ),
    }

    result = await collector(FakeFetcher(pages), FakeFirecrawl()).collect(
        site_url="https://example.com/",
        discovery_limit=2,
        deep_analysis_limit=1,
    )

    assert result.discovered_url_count == 2
    assert [str(page.url) for page in result.pages].count("https://example.com/about") == 1


@pytest.mark.anyio
async def test_page_type_counts_follow_fixed_taxonomy_order() -> None:
    pages = {
        "https://example.com/": html_response(
            "https://example.com/",
            '<a href="/about">About</a><a href="/services/plumbing">Service</a>',
        ),
        "https://example.com/about": html_response("https://example.com/about", "<h1>About</h1>"),
        "https://example.com/services/plumbing": html_response(
            "https://example.com/services/plumbing", "<h1>Plumbing</h1>"
        ),
    }

    result = await collector(FakeFetcher(pages), FakeFirecrawl()).collect(
        site_url="https://example.com/",
        discovery_limit=3,
        deep_analysis_limit=1,
    )

    assert [count.label for count in result.page_type_counts] == [
        "home",
        "service_detail",
        "about",
    ]


@pytest.mark.anyio
async def test_truncates_at_limit_and_records_limitation() -> None:
    root = '<a href="/a">A</a><a href="/b">B</a><a href="/c">C</a>'
    pages = {
        "https://example.com/": html_response("https://example.com/", root),
        "https://example.com/a": html_response("https://example.com/a", "A"),
    }

    result = await collector(FakeFetcher(pages), FakeFirecrawl()).collect(
        site_url="https://example.com/",
        discovery_limit=2,
        deep_analysis_limit=1,
    )

    assert result.discovered_url_count == 2
    assert "discovery_limit_reached" in result.limitations


@pytest.mark.anyio
async def test_uses_firecrawl_once_when_native_inventory_is_short() -> None:
    pages = {
        "https://example.com/": html_response("https://example.com/", "<h1>Home</h1>"),
        "https://example.com/a": html_response("https://example.com/a", "<h1>A</h1>"),
        "https://example.com/b": html_response("https://example.com/b", "<h1>B</h1>"),
    }
    firecrawl = FakeFirecrawl(("https://example.com/a", "https://example.com/b"))

    result = await collector(FakeFetcher(pages), firecrawl).collect(
        site_url="https://example.com/",
        discovery_limit=3,
        deep_analysis_limit=1,
    )

    assert result.discovered_url_count == 3
    assert firecrawl.calls == 1
    assert any("firecrawl_map" in page.discovery_sources for page in result.pages[1:])


@pytest.mark.anyio
async def test_preserves_individual_page_failure_without_aborting() -> None:
    pages = {
        "https://example.com/": html_response(
            "https://example.com/",
            '<a href="/slow">Slow</a>',
        ),
        "https://example.com/slow": SiteFetchError("page_timeout", "timed out"),
    }

    result = await collector(FakeFetcher(pages), FakeFirecrawl()).collect(
        site_url="https://example.com/",
        discovery_limit=2,
        deep_analysis_limit=1,
    )

    assert result.discovered_url_count == 2
    assert result.structurally_checked_count == 1
    assert result.pages[1].check_status == "failed"
    assert result.pages[1].error_code == "page_timeout"


@pytest.mark.anyio
async def test_root_failure_is_deterministic() -> None:
    fetcher = FakeFetcher(
        {"https://example.com/": SiteFetchError("page_timeout", "timed out")}
    )

    with pytest.raises(SiteInventoryCollectionError) as exc_info:
        await collector(fetcher).collect(
            site_url="https://example.com/",
            discovery_limit=2,
            deep_analysis_limit=1,
        )

    assert exc_info.value.code == "root_unreachable"


@pytest.mark.anyio
async def test_deep_failure_uses_next_ranked_page_as_replacement() -> None:
    root_html = '<h1>Home</h1><a href="/services/plumbing">Service</a><a href="/about">About</a>'
    pages = {
        "https://example.com/": [
            html_response("https://example.com/", root_html),
            html_response("https://example.com/", root_html),
        ],
        "https://example.com/services/plumbing": [
            html_response("https://example.com/services/plumbing", "<h1>Plumbing</h1>"),
            SiteFetchError("page_timeout", "deep timeout"),
        ],
        "https://example.com/about": [
            html_response("https://example.com/about", "<h1>About</h1>"),
            html_response("https://example.com/about", "<h1>About</h1>"),
        ],
    }
    fetcher = FakeFetcher(pages)
    deep_collector = SiteInventoryCollector(
        fetcher=fetcher,
        firecrawl=FakeFirecrawl(),
        structural_max_bytes=1,
        deep_max_bytes=2_000_000,
        sitemap_max_bytes=100_000,
        sitemap_decompressed_max_bytes=500_000,
        sitemap_max_files=5,
        sitemap_max_depth=2,
        batch_size=2,
        clock=lambda: NOW,
    )

    result = await deep_collector.collect(
        site_url="https://example.com/",
        discovery_limit=3,
        deep_analysis_limit=2,
        primary_service="plumbing",
        target_market="Austin",
    )

    assert result.deep_analyzed_count == 2
    assert [str(page.url) for page in result.selected_pages] == [
        "https://example.com/",
        "https://example.com/about",
    ]
    assert len(result.deep_attempts) == 3
    assert result.deep_attempts[1].error_code == "page_timeout"
    assert result.deep_attempts[1].selected_final is False
    assert "deep_page_failures_present" in result.limitations
