"""Orchestration for bounded v2.2 full-site inventory collection."""

from __future__ import annotations

import asyncio
import hashlib
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol, cast

from app.collectors.site_inventory_discovery import (
    RobotsPolicy,
    discover_sitemap_urls,
    load_robots,
)
from app.collectors.site_inventory_fetcher import (
    BoundedSiteFetcher,
    SiteFetchError,
    SiteFetchResponse,
)
from app.collectors.site_inventory_firecrawl import FirecrawlMapResult
from app.collectors.site_inventory_html import HtmlStructure, extract_html_structure
from app.collectors.site_inventory_models import (
    DISCOVERY_SOURCE_ORDER,
    SITE_PAGE_TYPE_ORDER,
    DeepPageAttempt,
    DeepPageSnapshot,
    DiscoverySource,
    GscPagePriority,
    InventoryCount,
    InventoryErrorCode,
    InventoryPageRecord,
    SelectedPageRecord,
    SiteInventorySnapshot,
)
from app.collectors.site_inventory_selection import RankedPage, classify_page, select_key_pages
from app.collectors.site_inventory_urls import SiteScope, canonicalize_inventory_url
from app.preflight_v22.urls import UrlSafetyError, UrlUnreachableError


Clock = Callable[[], datetime]
_HTML_TYPES = {"text/html", "application/xhtml+xml"}


class FirecrawlMapper(Protocol):
    async def map(self, root_url: str, *, limit: int) -> FirecrawlMapResult: ...


class SiteInventoryCollectionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.user_message = message


@dataclass
class _Candidate:
    url: str
    sources: list[DiscoverySource]
    crawl_depth: int
    prefetched: SiteFetchResponse | None = None


@dataclass(frozen=True)
class _CheckedCandidate:
    candidate: _Candidate
    record: InventoryPageRecord
    structure: HtmlStructure | None
    response: SiteFetchResponse | None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _error_code(exc: BaseException) -> InventoryErrorCode:
    if isinstance(exc, SiteFetchError) and exc.code in {
        "unsafe_target",
        "page_timeout",
        "response_too_large",
        "content_unsupported",
        "http_error",
    }:
        return cast(InventoryErrorCode, exc.code)
    if isinstance(exc, UrlSafetyError):
        return "unsafe_target"
    return "http_error"


class SiteInventoryCollector:
    def __init__(
        self,
        *,
        fetcher: BoundedSiteFetcher,
        firecrawl: FirecrawlMapper | None,
        structural_max_bytes: int,
        sitemap_max_bytes: int,
        sitemap_decompressed_max_bytes: int,
        sitemap_max_files: int,
        sitemap_max_depth: int,
        batch_size: int,
        deep_max_bytes: int = 2_000_000,
        clock: Clock = _utc_now,
    ) -> None:
        self.fetcher = fetcher
        self.firecrawl = firecrawl
        self.structural_max_bytes = structural_max_bytes
        self.sitemap_max_bytes = sitemap_max_bytes
        self.sitemap_decompressed_max_bytes = sitemap_decompressed_max_bytes
        self.sitemap_max_files = sitemap_max_files
        self.sitemap_max_depth = sitemap_max_depth
        self.batch_size = batch_size
        self.deep_max_bytes = deep_max_bytes
        self.clock = clock

    async def collect(
        self,
        *,
        site_url: str,
        discovery_limit: int,
        deep_analysis_limit: int,
        primary_service: str = "",
        target_market: str = "",
        gsc_priorities: list[GscPagePriority] | None = None,
    ) -> SiteInventorySnapshot:
        discovery_limit = min(max(discovery_limit, 1), 500)
        deep_analysis_limit = min(max(deep_analysis_limit, 1), 50)
        started_at = self.clock()
        initial_scope = SiteScope.from_root(site_url)
        try:
            root_response = await self.fetcher.fetch(
                initial_scope.root_url,
                scope=initial_scope,
                max_bytes=self.structural_max_bytes,
                accepted_media_types=_HTML_TYPES,
            )
        except (SiteFetchError, UrlUnreachableError) as exc:
            raise SiteInventoryCollectionError(
                "root_unreachable",
                "The site homepage could not be reached for inventory collection.",
            ) from exc
        except UrlSafetyError as exc:
            raise SiteInventoryCollectionError(
                "unsafe_target",
                "The site homepage is not a safe public target.",
            ) from exc
        if root_response.status_code >= 400:
            raise SiteInventoryCollectionError(
                "root_unreachable",
                "The site homepage returned an HTTP error.",
            )

        scope = SiteScope.from_root(root_response.final_url)
        robots = await load_robots(
            self.fetcher,
            scope=scope,
            max_bytes=min(self.sitemap_max_bytes, 1_000_000),
        )
        limitations = list(robots.limitations)

        candidates: dict[str, _Candidate] = {}
        queue: deque[str] = deque()
        records: dict[str, InventoryPageRecord] = {}
        structural_responses: dict[str, SiteFetchResponse] = {}
        truncated = False

        def add_candidate(
            raw_url: str,
            source: DiscoverySource,
            depth: int,
            *,
            base_url: str | None = None,
            prefetched: SiteFetchResponse | None = None,
        ) -> bool:
            nonlocal truncated
            normalized = canonicalize_inventory_url(
                raw_url,
                base_url=base_url,
                scope=scope,
            )
            if normalized is None or (source != "seed" and not robots.allowed(normalized)):
                return False
            if normalized in records:
                current = records[normalized]
                if source not in current.discovery_sources:
                    records[normalized] = current.model_copy(
                        update={"discovery_sources": [*current.discovery_sources, source]}
                    )
                return True
            if normalized in candidates:
                current = candidates[normalized]
                if source not in current.sources:
                    current.sources.append(source)
                current.crawl_depth = min(current.crawl_depth, depth)
                if prefetched is not None:
                    current.prefetched = prefetched
                return True
            if len(records) + len(candidates) >= discovery_limit:
                truncated = True
                return False
            candidates[normalized] = _Candidate(
                url=normalized,
                sources=[source],
                crawl_depth=depth,
                prefetched=prefetched,
            )
            queue.append(normalized)
            return True

        add_candidate(scope.root_url, "seed", 0, prefetched=root_response)

        declared_sitemaps = list(robots.sitemaps)
        if declared_sitemaps and len(candidates) < discovery_limit:
            result = await discover_sitemap_urls(
                self.fetcher,
                scope=scope,
                sitemap_urls=declared_sitemaps,
                robots=robots,
                max_files=self.sitemap_max_files,
                max_depth=self.sitemap_max_depth,
                max_bytes=self.sitemap_max_bytes,
                max_decompressed_bytes=self.sitemap_decompressed_max_bytes,
                page_limit=discovery_limit,
            )
            limitations.extend(result.limitations)
            for url in result.page_urls:
                add_candidate(url, "robots_sitemap", 1)

        default_sitemap = f"{scope.root_url.rstrip('/')}/sitemap.xml"
        if default_sitemap not in declared_sitemaps and len(candidates) < discovery_limit:
            result = await discover_sitemap_urls(
                self.fetcher,
                scope=scope,
                sitemap_urls=[default_sitemap],
                robots=robots,
                max_files=self.sitemap_max_files,
                max_depth=self.sitemap_max_depth,
                max_bytes=self.sitemap_max_bytes,
                max_decompressed_bytes=self.sitemap_decompressed_max_bytes,
                page_limit=discovery_limit,
            )
            limitations.extend(result.limitations)
            for url in result.page_urls:
                add_candidate(url, "default_sitemap", 1)

        await self._drain_candidates(
            scope=scope,
            robots=robots,
            candidates=candidates,
            queue=queue,
            records=records,
            structural_responses=structural_responses,
            add_candidate=add_candidate,
        )

        if len(records) < discovery_limit:
            if self.firecrawl is None:
                limitations.append("firecrawl_unavailable")
            else:
                firecrawl_result = await self.firecrawl.map(
                    scope.root_url,
                    limit=discovery_limit - len(records),
                )
                if firecrawl_result.limitation:
                    limitations.append(firecrawl_result.limitation)
                for url in firecrawl_result.urls:
                    add_candidate(url, "firecrawl_map", 1)
                await self._drain_candidates(
                    scope=scope,
                    robots=robots,
                    candidates=candidates,
                    queue=queue,
                    records=records,
                    structural_responses=structural_responses,
                    add_candidate=add_candidate,
                )

        if truncated:
            limitations.append("discovery_limit_reached")
        if not records or not any(page.check_status == "checked" for page in records.values()):
            raise SiteInventoryCollectionError(
                "empty_inventory",
                "No usable site pages were available for inventory collection.",
            )
        if any(page.check_status != "checked" for page in records.values()):
            limitations.append("page_failures_present")

        pages = list(records.values())[:discovery_limit]
        checked_pages = [page for page in pages if page.check_status == "checked"]
        ranked_pages = select_key_pages(
            checked_pages,
            limit=len(checked_pages),
            primary_service=primary_service,
            target_market=target_market,
            gsc_priorities=gsc_priorities or [],
        )
        selected_pages, deep_attempts, deep_limitations = await self._collect_deep_pages(
            ranked_pages,
            scope=scope,
            structural_responses=structural_responses,
            limit=deep_analysis_limit,
        )
        limitations.extend(deep_limitations)
        page_types = Counter(page.page_type for page in checked_pages)
        sources: Counter[str] = Counter()
        for page in pages:
            sources.update(page.discovery_sources)
        unique_limitations = list(dict.fromkeys(limitations))
        return SiteInventorySnapshot(
            schema_version="site_inventory_snapshot_v1",
            root_url=scope.root_url,
            canonical_host=scope.canonical_host,
            started_at=started_at,
            completed_at=self.clock(),
            discovery_limit=discovery_limit,
            deep_analysis_limit=deep_analysis_limit,
            discovered_url_count=len(pages),
            structurally_checked_count=len(checked_pages),
            deep_analyzed_count=sum(page.deep_analyzed for page in selected_pages),
            pages=pages,
            selected_pages=selected_pages,
            deep_attempts=deep_attempts,
            page_type_counts=[
                InventoryCount(label=label, count=count)
                for label in SITE_PAGE_TYPE_ORDER
                if (count := page_types.get(label, 0))
            ],
            source_counts=[
                InventoryCount(label=label, count=count)
                for label in DISCOVERY_SOURCE_ORDER
                if (count := sources.get(label, 0))
            ],
            limitations=unique_limitations,
        )

    async def _drain_candidates(
        self,
        *,
        scope: SiteScope,
        robots: RobotsPolicy,
        candidates: dict[str, _Candidate],
        queue: deque[str],
        records: dict[str, InventoryPageRecord],
        structural_responses: dict[str, SiteFetchResponse],
        add_candidate: Callable[..., bool],
    ) -> None:
        while queue:
            batch: list[_Candidate] = []
            while queue and len(batch) < self.batch_size:
                url = queue.popleft()
                candidate = candidates.pop(url, None)
                if candidate is not None:
                    batch.append(candidate)
            if not batch:
                continue
            checked_batch = await asyncio.gather(
                *(self._check_candidate(candidate, scope=scope) for candidate in batch)
            )
            for checked in checked_batch:
                candidate = checked.candidate
                record = checked.record
                chosen_url = candidate.url
                if "seed" not in candidate.sources and record.canonical_url is not None:
                    chosen_url = str(record.canonical_url)

                pending_alias = candidates.pop(chosen_url, None) if chosen_url != candidate.url else None
                sources = list(candidate.sources)
                depth = candidate.crawl_depth
                if pending_alias is not None:
                    for source in pending_alias.sources:
                        if source not in sources:
                            sources.append(source)
                    depth = min(depth, pending_alias.crawl_depth)

                if chosen_url in records:
                    existing = records[chosen_url]
                    merged_sources = list(existing.discovery_sources)
                    for source in sources:
                        if source not in merged_sources:
                            merged_sources.append(source)
                    records[chosen_url] = existing.model_copy(
                        update={
                            "discovery_sources": merged_sources,
                            "crawl_depth": min(existing.crawl_depth, depth),
                        }
                    )
                    continue

                if chosen_url != str(record.url):
                    record = InventoryPageRecord.model_validate(
                        {**record.model_dump(mode="json"), "url": chosen_url}
                    )
                if sources != record.discovery_sources or depth != record.crawl_depth:
                    record = record.model_copy(
                        update={"discovery_sources": sources, "crawl_depth": depth}
                    )
                records[chosen_url] = record
                if checked.response is not None:
                    structural_responses[chosen_url] = checked.response

                if checked.structure is not None and "nofollow" not in checked.structure.meta_robots:
                    for link in checked.structure.internal_links:
                        add_candidate(
                            link,
                            "internal_link",
                            record.crawl_depth + 1,
                            base_url=str(record.final_url or record.url),
                        )

    async def _check_candidate(
        self,
        candidate: _Candidate,
        *,
        scope: SiteScope,
    ) -> _CheckedCandidate:
        try:
            response = candidate.prefetched or await self.fetcher.fetch(
                candidate.url,
                scope=scope,
                max_bytes=self.structural_max_bytes,
                accepted_media_types=_HTML_TYPES,
            )
            structure = extract_html_structure(
                response.decode(),
                page_url=response.final_url,
                scope=scope,
            )
            classification = classify_page(
                url=candidate.url,
                root_url=scope.root_url,
                structure=structure,
            )
            record = InventoryPageRecord(
                url=candidate.url,
                discovery_sources=candidate.sources,
                crawl_depth=candidate.crawl_depth,
                check_status="checked",
                status_code=response.status_code,
                final_url=response.final_url,
                content_type=response.content_type[:200] or "application/octet-stream",
                response_bytes=response.response_bytes,
                title=structure.title,
                h1=structure.h1,
                canonical_url=structure.canonical_url,
                meta_robots=list(structure.meta_robots),
                schema_types=list(structure.schema_types),
                internal_links=list(structure.internal_links),
                page_type=classification.page_type,
                classification_reasons=list(classification.reasons),
                error_code=None,
            )
            return _CheckedCandidate(candidate, record, structure, response)
        except (SiteFetchError, UrlSafetyError, UrlUnreachableError) as exc:
            record = InventoryPageRecord(
                url=candidate.url,
                discovery_sources=candidate.sources,
                crawl_depth=candidate.crawl_depth,
                check_status="failed",
                response_bytes=0,
                meta_robots=[],
                schema_types=[],
                internal_links=[],
                page_type="other",
                classification_reasons=["check_failed"],
                error_code=_error_code(exc),
            )
            return _CheckedCandidate(candidate, record, None, None)

    async def _collect_deep_pages(
        self,
        ranked_pages: list[RankedPage],
        *,
        scope: SiteScope,
        structural_responses: dict[str, SiteFetchResponse],
        limit: int,
    ) -> tuple[list[SelectedPageRecord], list[DeepPageAttempt], list[str]]:
        outcomes: list[
            tuple[int, RankedPage, DeepPageSnapshot | None, InventoryErrorCode | None]
        ] = []
        success_count = 0
        for rank, ranked in enumerate(ranked_pages, start=1):
            if success_count >= limit:
                break
            try:
                snapshot = await self._deep_snapshot(
                    ranked,
                    scope=scope,
                    cached=structural_responses.get(str(ranked.page.url)),
                )
            except (SiteFetchError, UrlSafetyError, UrlUnreachableError) as exc:
                outcomes.append((rank, ranked, None, _error_code(exc)))
            else:
                outcomes.append((rank, ranked, snapshot, None))
                success_count += 1

        successful = [outcome for outcome in outcomes if outcome[2] is not None]
        failed = [outcome for outcome in outcomes if outcome[2] is None]
        failed_fill = failed[: max(0, limit - len(successful))]
        final_outcomes = sorted([*successful, *failed_fill], key=lambda item: item[0])
        final_urls = {str(outcome[1].page.url) for outcome in final_outcomes}

        selected = [
            SelectedPageRecord(
                url=ranked.page.url,
                page_type=ranked.page.page_type,
                crawl_depth=ranked.page.crawl_depth,
                selection_score=ranked.score,
                selection_reasons=list(ranked.reasons),
                deep_analyzed=snapshot is not None,
                deep_snapshot=snapshot,
            )
            for _, ranked, snapshot, _ in final_outcomes
        ]
        attempts = [
            DeepPageAttempt(
                url=ranked.page.url,
                rank=rank,
                succeeded=snapshot is not None,
                selected_final=str(ranked.page.url) in final_urls,
                error_code=error_code,
            )
            for rank, ranked, snapshot, error_code in outcomes
        ]
        limitations: list[str] = []
        if len(successful) < limit:
            limitations.append("candidate_exhausted")
        if failed:
            limitations.append("deep_page_failures_present")
        return selected, attempts, limitations

    async def _deep_snapshot(
        self,
        ranked: RankedPage,
        *,
        scope: SiteScope,
        cached: SiteFetchResponse | None,
    ) -> DeepPageSnapshot:
        response = cached
        if (
            response is None
            or response.response_bytes >= self.structural_max_bytes
            or response.status_code >= 400
        ):
            response = await self.fetcher.fetch(
                str(ranked.page.url),
                scope=scope,
                max_bytes=self.deep_max_bytes,
                accepted_media_types=_HTML_TYPES,
            )
        if response.status_code >= 400:
            raise SiteFetchError("http_error", "The deep page returned an HTTP error.")
        html = response.decode()
        structure = extract_html_structure(
            html,
            page_url=response.final_url,
            scope=scope,
            max_text_chars=2_000_000,
        )
        return DeepPageSnapshot(
            url=ranked.page.url,
            final_url=response.final_url,
            page_type=ranked.page.page_type,
            crawl_depth=ranked.page.crawl_depth,
            collected_at=self.clock(),
            status_code=response.status_code,
            content_type=response.content_type[:200] or "application/octet-stream",
            response_bytes=response.response_bytes,
            content_checksum=f"sha256:{hashlib.sha256(response.body).hexdigest()}",
            html=html,
            text=structure.visible_text,
            title=structure.title,
            h1=structure.h1,
        )
