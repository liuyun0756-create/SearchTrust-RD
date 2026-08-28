import gzip

import pytest

from app.collectors.site_inventory_discovery import (
    RobotsPolicy,
    SitemapParseError,
    discover_sitemap_urls,
    parse_robots,
    parse_sitemap_document,
)
from app.collectors.site_inventory_fetcher import SiteFetchResponse
from app.collectors.site_inventory_urls import SiteScope


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def response(url: str, body: bytes, *, status: int = 200, content_type: str = "application/xml") -> SiteFetchResponse:
    return SiteFetchResponse(
        requested_url=url,
        final_url=url,
        status_code=status,
        content_type=content_type,
        body=body,
    )


def test_parses_robots_specific_rules_sitemaps_and_delay() -> None:
    policy = parse_robots(
        """
        User-agent: *
        Disallow: /private
        User-agent: SearchTrust-SiteInventory
        Disallow: /account
        Crawl-delay: 2
        Sitemap: https://example.com/sitemap.xml
        """,
        root_url="https://example.com/",
    )

    assert policy.allowed("https://example.com/services") is True
    assert policy.allowed("https://example.com/account/profile") is False
    assert policy.sitemaps == ("https://example.com/sitemap.xml",)
    assert policy.crawl_delay == 2


def test_robots_allow_all_policy_is_explicit() -> None:
    policy = RobotsPolicy.allow_all(root_url="https://example.com/", limitation="robots_unavailable")

    assert policy.allowed("https://example.com/anything") is True
    assert policy.limitations == ("robots_unavailable",)


def test_parses_urlset_and_filters_out_of_scope_pages() -> None:
    document = parse_sitemap_document(
        b"""<?xml version="1.0"?>
        <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
          <url><loc>https://example.com/services/</loc></url>
          <url><loc>https://www.example.com/services?utm_source=x</loc></url>
          <url><loc>https://other.example/page</loc></url>
        </urlset>""",
        source_url="https://example.com/sitemap.xml",
        scope=SiteScope.from_root("https://example.com/"),
        max_decompressed_bytes=10_000,
    )

    assert document.page_urls == ("https://example.com/services",)
    assert document.child_sitemaps == ()


def test_parses_gzip_sitemap_index() -> None:
    body = gzip.compress(
        b"""<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
        <sitemap><loc>https://example.com/sitemap-pages.xml</loc></sitemap>
        <sitemap><loc>https://other.example/sitemap.xml</loc></sitemap>
        </sitemapindex>"""
    )

    document = parse_sitemap_document(
        body,
        source_url="https://example.com/sitemap.xml.gz",
        scope=SiteScope.from_root("https://example.com/"),
        max_decompressed_bytes=10_000,
    )

    assert document.page_urls == ()
    assert document.child_sitemaps == ("https://example.com/sitemap-pages.xml",)


@pytest.mark.parametrize(
    "body",
    [
        b'<!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><urlset>&xxe;</urlset>',
        gzip.compress(b"x" * 101),
        b"<not-a-sitemap />",
    ],
)
def test_rejects_unsafe_or_invalid_sitemap_documents(body: bytes) -> None:
    with pytest.raises(SitemapParseError):
        parse_sitemap_document(
            body,
            source_url="https://example.com/sitemap.xml",
            scope=SiteScope.from_root("https://example.com/"),
            max_decompressed_bytes=100,
        )


@pytest.mark.anyio
async def test_discovers_bounded_sitemap_tree_in_stable_order() -> None:
    documents = {
        "https://example.com/sitemap.xml": response(
            "https://example.com/sitemap.xml",
            b"""<sitemapindex><sitemap><loc>https://example.com/a.xml</loc></sitemap>
            <sitemap><loc>https://example.com/b.xml</loc></sitemap></sitemapindex>""",
        ),
        "https://example.com/a.xml": response(
            "https://example.com/a.xml",
            b"<urlset><url><loc>https://example.com/a</loc></url></urlset>",
        ),
        "https://example.com/b.xml": response(
            "https://example.com/b.xml",
            b"<urlset><url><loc>https://example.com/b</loc></url></urlset>",
        ),
    }

    class FakeFetcher:
        async def fetch(self, url: str, **_: object) -> SiteFetchResponse:
            return documents[url]

    result = await discover_sitemap_urls(
        FakeFetcher(),
        scope=SiteScope.from_root("https://example.com/"),
        sitemap_urls=["https://example.com/sitemap.xml"],
        robots=RobotsPolicy.allow_all(root_url="https://example.com/"),
        max_files=3,
        max_depth=2,
        max_bytes=10_000,
        max_decompressed_bytes=10_000,
        page_limit=10,
    )

    assert result.page_urls == ("https://example.com/a", "https://example.com/b")
    assert result.fetched_sitemaps == 3
    assert result.limitations == ()


@pytest.mark.anyio
async def test_loading_robots_applies_crawl_delay() -> None:
    from app.collectors.site_inventory_discovery import load_robots

    class FakeFetcher:
        delay = None

        async def fetch(self, url: str, **_: object) -> SiteFetchResponse:
            return response(
                url,
                b"User-agent: SearchTrust-SiteInventory\nCrawl-delay: 3\n",
                content_type="text/plain",
            )

        def apply_crawl_delay(self, seconds: float | None) -> None:
            self.delay = seconds

    fetcher = FakeFetcher()
    policy = await load_robots(
        fetcher,
        scope=SiteScope.from_root("https://example.com/"),
        max_bytes=10_000,
    )

    assert policy.crawl_delay == 3
    assert fetcher.delay == 3
