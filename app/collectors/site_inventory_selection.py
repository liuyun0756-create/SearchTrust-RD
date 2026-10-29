"""Deterministic page classification and later deep-page selection rules."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit

from app.api.v2.models import FirstPartySnapshotEnvelope
from app.collectors.site_inventory_html import HtmlStructure
from app.collectors.site_inventory_models import (
    GscPagePriority,
    InventoryPageRecord,
    SitePageType,
)
from app.collectors.site_inventory_urls import SiteScope, canonicalize_inventory_url


@dataclass(frozen=True)
class ClassificationResult:
    page_type: SitePageType
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class RankedPage:
    page: InventoryPageRecord
    score: float
    reasons: tuple[str, ...]


def _path_segments(url: str) -> list[str]:
    return [segment.casefold() for segment in urlsplit(url).path.split("/") if segment]


def classify_page(
    *,
    url: str,
    root_url: str,
    structure: HtmlStructure,
) -> ClassificationResult:
    path = urlsplit(url).path.rstrip("/") or "/"
    root_path = urlsplit(root_url).path.rstrip("/") or "/"
    segments = _path_segments(url)
    joined_path = "/".join(segments)
    text = " ".join(filter(None, (structure.title, structure.h1))).casefold()
    schema_types = {item.casefold() for item in structure.schema_types}

    if path == root_path and urlsplit(url).hostname == urlsplit(root_url).hostname:
        return ClassificationResult("home", ("root_url",))
    if any(token in segments for token in {"privacy", "terms", "legal", "cookies"}):
        return ClassificationResult("legal", ("legal_path",))
    if "faqpage" in schema_types or "frequently asked" in text or "common questions" in text:
        return ClassificationResult("faq", ("faq_signal",))
    if "product" in schema_types:
        return ClassificationResult("product_detail", ("schema_product",))
    if schema_types.intersection({"blogposting", "newsarticle", "article"}):
        return ClassificationResult("blog_post", ("schema_article",))
    if any(token in segments for token in {"contact", "contact-us", "get-in-touch"}):
        return ClassificationResult("contact", ("contact_path",))
    if any(token in segments for token in {"about", "about-us", "our-story"}):
        return ClassificationResult("about", ("about_path",))
    if any(token in segments for token in {"team", "staff", "leadership", "people"}):
        return ClassificationResult("team", ("team_path",))
    if any(token in segments for token in {"reviews", "testimonials", "testimonial"}):
        return ClassificationResult("review_testimonial", ("review_path",))
    if any(
        token in segments
        for token in {"case-study", "case-studies", "portfolio", "projects", "our-work"}
    ):
        return ClassificationResult("case_study_portfolio", ("case_study_path",))
    if any(token in segments for token in {"service-area", "service-areas", "areas-we-serve"}):
        return ClassificationResult("service_area", ("service_area_path",))
    if any(token in segments for token in {"location", "locations", "branches", "stores"}):
        return ClassificationResult("location", ("location_path",))
    if segments and segments[-1] in {"services", "service", "what-we-do"}:
        return ClassificationResult("service_index", ("service_index_path",))
    if "services" in segments or "service" in schema_types:
        return ClassificationResult("service_detail", ("service_detail_signal",))
    if segments and segments[-1] in {"blog", "news", "insights", "resources"}:
        return ClassificationResult("blog_index", ("blog_index_path",))
    if any(token in segments for token in {"blog", "news", "insights"}):
        return ClassificationResult("blog_post", ("blog_path",))
    if segments and segments[-1] in {"products", "shop", "catalog"}:
        return ClassificationResult("product_category", ("product_index_path",))
    if any(token in segments for token in {"products", "product", "shop", "items"}):
        return ClassificationResult("product_detail", ("product_path",))
    return ClassificationResult("other", (f"unmatched_path:{joined_path or '/'}",))


_TYPE_SCORES: dict[SitePageType, float] = {
    "home": 10_000,
    "service_index": 700,
    "service_detail": 680,
    "service_area": 650,
    "location": 640,
    "about": 560,
    "contact": 550,
    "team": 520,
    "review_testimonial": 540,
    "case_study_portfolio": 530,
    "faq": 510,
    "blog_index": 260,
    "blog_post": 180,
    "product_category": 450,
    "product_detail": 300,
    "legal": 80,
    "other": 100,
}
_COVERAGE_ORDER: tuple[SitePageType, ...] = (
    "service_index",
    "service_detail",
    "service_area",
    "location",
    "about",
    "contact",
    "team",
    "review_testimonial",
    "case_study_portfolio",
    "faq",
    "product_category",
    "blog_index",
)


def _business_tokens(*values: str) -> set[str]:
    tokens: set[str] = set()
    for value in values:
        tokens.update(
            token for token in re.findall(r"[a-z0-9]+", value.casefold()) if len(token) >= 3
        )
    return tokens


def _rank_page(
    page: InventoryPageRecord,
    *,
    business_tokens: set[str],
    gsc_by_url: dict[str, GscPagePriority],
) -> RankedPage:
    score = _TYPE_SCORES[page.page_type] - min(page.crawl_depth, 20) * 2
    reasons = [f"page_type:{page.page_type}"]
    haystack = " ".join(
        value for value in (str(page.url), page.title or "", page.h1 or "") if value
    ).casefold()
    matches = sorted(token for token in business_tokens if token in haystack)
    if matches:
        score += min(len(matches), 5) * 50
        reasons.append("business_match")
    priority = gsc_by_url.get(str(page.url))
    if priority is not None:
        score += min(
            250,
            math.log1p(priority.impressions) * 12 + math.log1p(priority.clicks) * 24,
        )
        reasons.append("gsc_priority")
    if page.page_type == "home":
        reasons.append("home_required")
    return RankedPage(page=page, score=round(score, 6), reasons=tuple(reasons))


def select_key_pages(
    pages: list[InventoryPageRecord],
    *,
    limit: int,
    primary_service: str,
    target_market: str,
    gsc_priorities: list[GscPagePriority],
) -> list[RankedPage]:
    limit = min(max(limit, 0), 500)
    if limit == 0:
        return []
    gsc_by_url = {str(item.url): item for item in gsc_priorities}
    business_tokens = _business_tokens(primary_service, target_market)
    eligible = [
        page
        for page in pages
        if page.check_status == "checked"
        and (page.status_code or 500) < 400
        and "noindex" not in page.meta_robots
    ]
    ranked = sorted(
        (
            _rank_page(
                page,
                business_tokens=business_tokens,
                gsc_by_url=gsc_by_url,
            )
            for page in eligible
        ),
        key=lambda item: (-item.score, str(item.page.url)),
    )

    chosen: list[RankedPage] = []
    chosen_urls: set[str] = set()

    def choose(item: RankedPage) -> None:
        url = str(item.page.url)
        if url not in chosen_urls and len(chosen) < limit:
            chosen.append(item)
            chosen_urls.add(url)

    for item in ranked:
        if item.page.page_type == "home":
            choose(item)
            break
    for page_type in _COVERAGE_ORDER:
        if len(chosen) >= limit:
            break
        candidate = next((item for item in ranked if item.page.page_type == page_type), None)
        if candidate is not None:
            choose(candidate)

    capped_types = {
        "blog_post": max(2, limit // 5),
        "product_detail": max(2, limit // 5),
    }
    for item in ranked:
        if len(chosen) >= limit:
            break
        cap = capped_types.get(item.page.page_type)
        if cap is not None and sum(
            selected.page.page_type == item.page.page_type for selected in chosen
        ) >= cap:
            continue
        choose(item)
    return chosen


def gsc_priorities_from_snapshots(
    snapshots: list[FirstPartySnapshotEnvelope],
    *,
    scope: SiteScope,
    now: datetime,
) -> list[GscPagePriority]:
    aggregated: dict[str, tuple[float, float, datetime]] = {}
    for snapshot in snapshots:
        if (
            snapshot.source_type != "gsc"
            or snapshot.health_status != "healthy"
            or snapshot.identity_match_status != "matched"
            or (snapshot.expires_at is not None and snapshot.expires_at <= now)
        ):
            continue
        for row in snapshot.normalized_payload.rows:
            dimensions = {item.key.casefold(): item.value for item in row.dimensions}
            raw_url = next(
                (
                    dimensions[key]
                    for key in ("page", "url", "landing_page", "landingpage")
                    if key in dimensions
                ),
                None,
            )
            if raw_url is None:
                continue
            normalized = canonicalize_inventory_url(raw_url, scope=scope)
            if normalized is None:
                continue
            metrics = {item.key.casefold(): item.value for item in row.metrics}
            impressions = max(0.0, metrics.get("impressions", 0.0))
            clicks = max(0.0, metrics.get("clicks", 0.0))
            if impressions == 0 and clicks == 0:
                continue
            previous = aggregated.get(normalized)
            if previous is None:
                aggregated[normalized] = (impressions, clicks, snapshot.fetched_at)
            else:
                aggregated[normalized] = (
                    previous[0] + impressions,
                    previous[1] + clicks,
                    max(previous[2], snapshot.fetched_at),
                )
    return [
        GscPagePriority(
            url=url,
            impressions=values[0],
            clicks=values[1],
            observed_at=values[2],
        )
        for url, values in sorted(aggregated.items())
    ]
