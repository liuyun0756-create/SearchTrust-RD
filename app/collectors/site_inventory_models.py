"""Strict internal contracts for the bounded v2.2 site inventory collector."""

from __future__ import annotations

from collections import Counter
from typing import Literal

from pydantic import AwareDatetime, Field, HttpUrl, model_validator

from app.report_v22.models import StrictModel


SitePageType = Literal[
    "home",
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
    "blog_index",
    "blog_post",
    "product_category",
    "product_detail",
    "legal",
    "other",
]
DiscoverySource = Literal[
    "seed",
    "robots_sitemap",
    "default_sitemap",
    "internal_link",
    "firecrawl_map",
]
PageCheckStatus = Literal["checked", "failed", "robots_disallowed"]
InventoryErrorCode = Literal[
    "unsafe_target",
    "root_unreachable",
    "robots_unavailable",
    "sitemap_unavailable",
    "page_timeout",
    "response_too_large",
    "content_unsupported",
    "http_error",
    "parse_failed",
    "firecrawl_unavailable",
    "candidate_exhausted",
    "empty_inventory",
]


class InventoryCount(StrictModel):
    label: str = Field(min_length=1, max_length=100)
    count: int = Field(ge=0, le=10_000)


class GscPagePriority(StrictModel):
    url: HttpUrl
    impressions: float = Field(ge=0)
    clicks: float = Field(ge=0)
    observed_at: AwareDatetime


class InventoryPageRecord(StrictModel):
    url: HttpUrl
    discovery_sources: list[DiscoverySource] = Field(min_length=1, max_length=5)
    crawl_depth: int = Field(ge=0, le=100)
    check_status: PageCheckStatus
    status_code: int | None = Field(default=None, ge=100, le=599)
    final_url: HttpUrl | None = None
    content_type: str | None = Field(default=None, max_length=200)
    response_bytes: int = Field(default=0, ge=0, le=5_000_000)
    title: str | None = Field(default=None, max_length=500)
    h1: str | None = Field(default=None, max_length=500)
    canonical_url: HttpUrl | None = None
    meta_robots: list[str] = Field(default_factory=list, max_length=20)
    schema_types: list[str] = Field(default_factory=list, max_length=50)
    internal_links: list[HttpUrl] = Field(default_factory=list, max_length=500)
    page_type: SitePageType
    classification_reasons: list[str] = Field(default_factory=list, max_length=20)
    error_code: InventoryErrorCode | None = None

    @model_validator(mode="after")
    def validate_check_result(self) -> "InventoryPageRecord":
        if len(set(self.discovery_sources)) != len(self.discovery_sources):
            raise ValueError("discovery sources must be unique")
        link_values = [str(url) for url in self.internal_links]
        if len(set(link_values)) != len(link_values):
            raise ValueError("internal links must be unique")
        if self.check_status == "checked":
            if self.status_code is None or self.final_url is None or self.content_type is None:
                raise ValueError("checked pages require HTTP result fields")
            if self.error_code is not None:
                raise ValueError("checked pages cannot include an error code")
        elif self.error_code is None:
            raise ValueError("unchecked pages require an error code")
        return self


class DeepPageSnapshot(StrictModel):
    url: HttpUrl
    final_url: HttpUrl
    page_type: SitePageType
    crawl_depth: int = Field(ge=0, le=100)
    collected_at: AwareDatetime
    status_code: int = Field(ge=100, le=599)
    content_type: str = Field(min_length=1, max_length=200)
    response_bytes: int = Field(ge=0, le=5_000_000)
    content_checksum: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    html: str = Field(max_length=2_000_000)
    text: str = Field(max_length=2_000_000)
    title: str | None = Field(default=None, max_length=500)
    h1: str | None = Field(default=None, max_length=500)


class SelectedPageRecord(StrictModel):
    url: HttpUrl
    page_type: SitePageType
    crawl_depth: int = Field(ge=0, le=100)
    selection_score: float
    selection_reasons: list[str] = Field(min_length=1, max_length=20)
    deep_analyzed: bool
    deep_snapshot: DeepPageSnapshot | None = None

    @model_validator(mode="after")
    def validate_deep_state(self) -> "SelectedPageRecord":
        if self.deep_analyzed != (self.deep_snapshot is not None):
            raise ValueError("deep analyzed state must match deep snapshot presence")
        if self.deep_snapshot is not None and str(self.deep_snapshot.url) != str(self.url):
            raise ValueError("deep snapshot URL must match selected page URL")
        return self


class DeepPageAttempt(StrictModel):
    url: HttpUrl
    rank: int = Field(ge=1, le=500)
    succeeded: bool
    selected_final: bool
    error_code: InventoryErrorCode | None = None

    @model_validator(mode="after")
    def validate_attempt(self) -> "DeepPageAttempt":
        if self.succeeded and self.error_code is not None:
            raise ValueError("successful deep attempts cannot include an error code")
        if not self.succeeded and self.error_code is None:
            raise ValueError("failed deep attempts require an error code")
        return self


class SiteInventorySnapshot(StrictModel):
    schema_version: Literal["site_inventory_snapshot_v1"]
    root_url: HttpUrl
    canonical_host: str = Field(min_length=1, max_length=253)
    started_at: AwareDatetime
    completed_at: AwareDatetime
    discovery_limit: int = Field(ge=1, le=500)
    deep_analysis_limit: int = Field(ge=1, le=50)
    discovered_url_count: int = Field(ge=1, le=500)
    structurally_checked_count: int = Field(ge=1, le=500)
    deep_analyzed_count: int = Field(ge=0, le=50)
    pages: list[InventoryPageRecord] = Field(min_length=1, max_length=500)
    selected_pages: list[SelectedPageRecord] = Field(default_factory=list, max_length=50)
    deep_attempts: list[DeepPageAttempt] = Field(default_factory=list, max_length=500)
    page_type_counts: list[InventoryCount] = Field(default_factory=list, max_length=17)
    source_counts: list[InventoryCount] = Field(default_factory=list, max_length=5)
    limitations: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def validate_inventory_counts(self) -> "SiteInventorySnapshot":
        if self.completed_at < self.started_at:
            raise ValueError("inventory completion cannot precede start")
        if self.discovered_url_count != len(self.pages):
            raise ValueError("discovered count must match page records")

        page_urls = [str(page.url) for page in self.pages]
        if len(set(page_urls)) != len(page_urls):
            raise ValueError("inventory page URLs must be unique")

        checked_pages = [page for page in self.pages if page.check_status == "checked"]
        if self.structurally_checked_count != len(checked_pages):
            raise ValueError("structurally checked count must match checked page records")
        if self.structurally_checked_count > self.discovered_url_count:
            raise ValueError("structurally checked count cannot exceed discovered count")

        selected_urls = [str(page.url) for page in self.selected_pages]
        if len(set(selected_urls)) != len(selected_urls):
            raise ValueError("selected page URLs must be unique")
        checked_urls = {str(page.url) for page in checked_pages}
        if any(url not in checked_urls for url in selected_urls):
            raise ValueError("selected pages must have successful structural checks")
        if len(self.selected_pages) > self.deep_analysis_limit:
            raise ValueError("selected pages cannot exceed deep analysis limit")

        deep_count = sum(page.deep_analyzed for page in self.selected_pages)
        if self.deep_analyzed_count != deep_count:
            raise ValueError("deep analyzed count must match selected page records")
        if self.deep_analyzed_count > self.structurally_checked_count:
            raise ValueError("deep analyzed count cannot exceed structurally checked count")

        type_labels = [item.label for item in self.page_type_counts]
        if len(set(type_labels)) != len(type_labels):
            raise ValueError("page type count labels must be unique")
        expected_types = Counter(page.page_type for page in checked_pages)
        supplied_types = {item.label: item.count for item in self.page_type_counts if item.count}
        if supplied_types != dict(expected_types):
            raise ValueError("page type counts must match checked page records")

        source_labels = [item.label for item in self.source_counts]
        if len(set(source_labels)) != len(source_labels):
            raise ValueError("source count labels must be unique")
        return self
