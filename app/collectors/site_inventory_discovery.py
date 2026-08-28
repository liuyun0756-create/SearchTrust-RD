"""Robots and bounded sitemap discovery for the v2.2 site inventory."""

from __future__ import annotations

import gzip
import io
import xml.etree.ElementTree as ET
from collections import deque
from dataclasses import dataclass
from urllib import robotparser

from app.collectors.site_inventory_fetcher import BoundedSiteFetcher, SiteFetchError
from app.collectors.site_inventory_urls import (
    SiteScope,
    canonicalize_inventory_url,
    canonicalize_site_resource_url,
)
from app.preflight_v22.urls import UrlUnreachableError


USER_AGENT = "SearchTrust-SiteInventory"


class SitemapParseError(ValueError):
    pass


@dataclass(frozen=True)
class RobotsPolicy:
    root_url: str
    parser: robotparser.RobotFileParser | None
    sitemaps: tuple[str, ...]
    crawl_delay: float | None
    limitations: tuple[str, ...]

    @classmethod
    def allow_all(
        cls,
        *,
        root_url: str,
        limitation: str | None = None,
    ) -> "RobotsPolicy":
        return cls(
            root_url=root_url,
            parser=None,
            sitemaps=(),
            crawl_delay=None,
            limitations=(limitation,) if limitation else (),
        )

    def allowed(self, url: str) -> bool:
        return True if self.parser is None else self.parser.can_fetch(USER_AGENT, url)


@dataclass(frozen=True)
class SitemapDocument:
    page_urls: tuple[str, ...]
    child_sitemaps: tuple[str, ...]


@dataclass(frozen=True)
class SitemapDiscoveryResult:
    page_urls: tuple[str, ...]
    fetched_sitemaps: int
    limitations: tuple[str, ...]


def parse_robots(text: str, *, root_url: str) -> RobotsPolicy:
    scope = SiteScope.from_root(root_url)
    parser = robotparser.RobotFileParser()
    parser.set_url(f"{scope.root_url.rstrip('/')}/robots.txt")
    parser.parse(text.splitlines())
    sitemaps: list[str] = []
    for raw_url in parser.site_maps() or []:
        normalized = canonicalize_site_resource_url(raw_url, scope=scope)
        if normalized is not None and normalized not in sitemaps:
            sitemaps.append(normalized)
    delay = parser.crawl_delay(USER_AGENT)
    if delay is None:
        delay = parser.crawl_delay("*")
    return RobotsPolicy(
        root_url=scope.root_url,
        parser=parser,
        sitemaps=tuple(sitemaps),
        crawl_delay=float(delay) if delay is not None else None,
        limitations=(),
    )


async def load_robots(
    fetcher: BoundedSiteFetcher,
    *,
    scope: SiteScope,
    max_bytes: int,
) -> RobotsPolicy:
    robots_url = f"{scope.root_url.rstrip('/')}/robots.txt"
    try:
        result = await fetcher.fetch(robots_url, scope=scope, max_bytes=max_bytes)
    except (SiteFetchError, UrlUnreachableError):
        return RobotsPolicy.allow_all(root_url=scope.root_url, limitation="robots_unavailable")
    if result.status_code in {404, 410}:
        return RobotsPolicy.allow_all(root_url=scope.root_url)
    if result.status_code >= 400:
        return RobotsPolicy.allow_all(root_url=scope.root_url, limitation="robots_unavailable")
    policy = parse_robots(result.decode(), root_url=scope.root_url)
    fetcher.apply_crawl_delay(policy.crawl_delay)
    return policy


def _bounded_gzip_decompress(body: bytes, *, max_bytes: int) -> bytes:
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(body)) as compressed:
            result = compressed.read(max_bytes + 1)
    except (EOFError, OSError) as exc:
        raise SitemapParseError("invalid gzip sitemap") from exc
    if len(result) > max_bytes:
        raise SitemapParseError("decompressed sitemap exceeded the size limit")
    return result


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].casefold()


def parse_sitemap_document(
    body: bytes,
    *,
    source_url: str,
    scope: SiteScope,
    max_decompressed_bytes: int,
    max_nodes: int = 20_000,
) -> SitemapDocument:
    payload = (
        _bounded_gzip_decompress(body, max_bytes=max_decompressed_bytes)
        if body.startswith(b"\x1f\x8b")
        else body
    )
    if len(payload) > max_decompressed_bytes:
        raise SitemapParseError("sitemap exceeded the size limit")
    lowered = payload.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise SitemapParseError("XML declarations with entities are not allowed")
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise SitemapParseError("invalid sitemap XML") from exc
    if sum(1 for _ in root.iter()) > max_nodes:
        raise SitemapParseError("sitemap exceeded the node limit")

    root_name = _local_name(root.tag)
    page_urls: list[str] = []
    child_sitemaps: list[str] = []
    if root_name not in {"urlset", "sitemapindex"}:
        raise SitemapParseError("unsupported sitemap root element")
    for element in root.iter():
        if _local_name(element.tag) != "loc" or not element.text:
            continue
        if root_name == "urlset":
            normalized = canonicalize_inventory_url(
                element.text,
                base_url=source_url,
                scope=scope,
            )
            if normalized is not None and normalized not in page_urls:
                page_urls.append(normalized)
        else:
            normalized = canonicalize_site_resource_url(
                element.text,
                base_url=source_url,
                scope=scope,
            )
            if normalized is not None and normalized not in child_sitemaps:
                child_sitemaps.append(normalized)
    return SitemapDocument(tuple(page_urls), tuple(child_sitemaps))


async def discover_sitemap_urls(
    fetcher: BoundedSiteFetcher,
    *,
    scope: SiteScope,
    sitemap_urls: list[str] | tuple[str, ...],
    robots: RobotsPolicy,
    max_files: int,
    max_depth: int,
    max_bytes: int,
    max_decompressed_bytes: int,
    page_limit: int,
) -> SitemapDiscoveryResult:
    queue: deque[tuple[str, int]] = deque()
    for raw_url in sitemap_urls:
        normalized = canonicalize_site_resource_url(raw_url, scope=scope)
        if normalized is not None and normalized not in {url for url, _ in queue}:
            queue.append((normalized, 0))

    visited: set[str] = set()
    pages: list[str] = []
    limitations: list[str] = []
    while queue and len(visited) < max_files and len(pages) < page_limit:
        sitemap_url, depth = queue.popleft()
        if sitemap_url in visited:
            continue
        visited.add(sitemap_url)
        try:
            response = await fetcher.fetch(
                sitemap_url,
                scope=scope,
                max_bytes=max_bytes,
            )
            if response.status_code >= 400:
                raise SiteFetchError("http_error", "The sitemap returned an HTTP error.")
            document = parse_sitemap_document(
                response.body,
                source_url=response.final_url,
                scope=scope,
                max_decompressed_bytes=max_decompressed_bytes,
            )
        except (SiteFetchError, SitemapParseError, UrlUnreachableError):
            if "sitemap_unavailable" not in limitations:
                limitations.append("sitemap_unavailable")
            continue

        for page_url in document.page_urls:
            if robots.allowed(page_url) and page_url not in pages:
                pages.append(page_url)
                if len(pages) >= page_limit:
                    break
        if depth < max_depth:
            queued_urls = {url for url, _ in queue}
            for child_url in document.child_sitemaps:
                if child_url not in visited and child_url not in queued_urls:
                    queue.append((child_url, depth + 1))
                    queued_urls.add(child_url)

    if queue and len(visited) >= max_files:
        limitations.append("sitemap_file_limit_reached")
    if len(pages) >= page_limit:
        limitations.append("sitemap_page_limit_reached")
    return SitemapDiscoveryResult(tuple(pages), len(visited), tuple(limitations))
