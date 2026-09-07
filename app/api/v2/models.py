"""Strict request and response contracts for the future SearchTrust API v2."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, HttpUrl, model_validator

from app.report_v22.contract_version import ContractVersion
from app.competitors_v22.limits import COMPETITOR_MAX_COUNT, COMPETITOR_MIN_COUNT
from app.report_v22.models import (
    BusinessIdentity,
    CompetitorId,
    HealthStatus,
    IdentityMatchStatus,
    ReportType,
    ReportV22,
    StrictModel,
    TargetMarket,
)


FirstPartySourceType = Literal["gsc", "gbp", "ga4"]
JobStatus = Literal["queued", "running", "succeeded", "failed"]
JobStage = Literal[
    "queued",
    "collecting_site",
    "collecting_market",
    "collecting_competitors",
    "building_evidence",
    "evaluating",
    "generating_copy",
    "validating",
    "persisting",
    "completed",
    "failed",
]


class PreflightRequest(StrictModel):
    site_url: HttpUrl
    gbp_url: HttpUrl | None = None
    primary_service: str | None = Field(default=None, min_length=1, max_length=200)
    target_market: TargetMarket | None = None


IdentityComparisonField = Literal["business_name", "phone", "address", "service_area"]
IdentityComparisonStatus = Literal["exact_match", "partial_match", "not_matched", "error"]


class IdentityFieldComparison(StrictModel):
    field: IdentityComparisonField
    site_value: str | None = Field(default=None, min_length=1, max_length=500)
    gbp_value: str | None = Field(default=None, min_length=1, max_length=500)
    status: IdentityComparisonStatus
    reason: str = Field(min_length=1, max_length=500)


class BusinessIdentityCandidate(StrictModel):
    business: BusinessIdentity
    confidence: Literal["low", "medium", "high"]
    match_reasons: list[str] = Field(min_length=1)
    requires_confirmation: bool
    field_comparisons: list[IdentityFieldComparison] = Field(min_length=4, max_length=4)

    @model_validator(mode="after")
    def validate_field_comparisons(self) -> "BusinessIdentityCandidate":
        expected = ["business_name", "phone", "address", "service_area"]
        actual = [item.field for item in self.field_comparisons]
        if actual != expected:
            raise ValueError("identity field comparisons must contain all four fields in contract order")
        return self


class TextCandidate(StrictModel):
    value: str = Field(min_length=1, max_length=240)
    confidence: Literal["low", "medium", "high"]
    evidence_summary: str = Field(min_length=1)


class MarketCandidate(StrictModel):
    market: TargetMarket
    confidence: Literal["low", "medium", "high"]
    evidence_summary: str = Field(min_length=1)


class CompetitorCandidate(StrictModel):
    competitor_id: CompetitorId
    business_name: str = Field(min_length=1, max_length=240)
    website_url: HttpUrl
    public_gbp_url: HttpUrl | None = None
    query_appearance_count: int = Field(ge=1)
    best_position: int = Field(ge=1)
    relevance_reason: str = Field(min_length=1)
    confidence: Literal["low", "medium", "high"]


class ModuleAvailability(StrictModel):
    module_key: Literal[
        "site_inventory",
        "site_deep_analysis",
        "serp_maps",
        "serp_local_pack",
        "serp_organic",
        "public_gbp",
        "competitor_analysis",
        "pagespeed",
    ]
    available: bool
    reason: str = Field(min_length=1)


class DataGap(StrictModel):
    gap_code: str = Field(min_length=1, max_length=100, pattern=r"^[A-Z0-9_]+$")
    message: str = Field(min_length=1)
    blocking: bool
    resolution: str = Field(min_length=1)


class PreflightResponse(StrictModel):
    preflight_id: UUID
    normalized_site_url: HttpUrl
    identity_candidates: list[BusinessIdentityCandidate] = Field(default_factory=list)
    service_candidates: list[TextCandidate] = Field(default_factory=list)
    market_candidates: list[MarketCandidate] = Field(default_factory=list)
    competitor_candidates: list[CompetitorCandidate] = Field(default_factory=list)
    module_availability: list[ModuleAvailability] = Field(min_length=1)
    data_gaps: list[DataGap] = Field(default_factory=list)
    estimated_duration_bucket: Literal["under_5_minutes", "5_to_10_minutes", "10_to_15_minutes"]
    coverage_summary: str = Field(min_length=1)


class DimensionValue(StrictModel):
    key: str = Field(min_length=1, max_length=120)
    value: str = Field(max_length=500)


class SnapshotMetric(StrictModel):
    key: str = Field(min_length=1, max_length=120)
    value: float
    unit: str = Field(min_length=1, max_length=40)


class SnapshotRow(StrictModel):
    dimensions: list[DimensionValue] = Field(default_factory=list)
    metrics: list[SnapshotMetric] = Field(min_length=1)


class NormalizedSnapshotPayload(StrictModel):
    rows: list[SnapshotRow] = Field(default_factory=list, max_length=1000)
    aggregates: list[SnapshotMetric] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class ProviderRequestContext(StrictModel):
    external_resource_id: str = Field(min_length=1, max_length=500)
    start_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    end_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    country_code: str | None = Field(default=None, pattern=r"^[A-Z]{2}$")
    device: Literal["desktop", "mobile", "tablet"] | None = None
    row_limit: int | None = Field(default=None, ge=1, le=1000)


class FirstPartySnapshotEnvelope(StrictModel):
    snapshot_id: UUID
    source_type: FirstPartySourceType
    schema_version: str = Field(min_length=1, max_length=64)
    fetched_at: AwareDatetime
    expires_at: AwareDatetime | None = None
    identity_match_status: IdentityMatchStatus
    health_status: HealthStatus
    health_reasons: list[str] = Field(default_factory=list)
    normalized_payload: NormalizedSnapshotPayload
    provider_request_context: ProviderRequestContext
    payload_checksum: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_expiry(self) -> "FirstPartySnapshotEnvelope":
        if self.expires_at and self.expires_at <= self.fetched_at:
            raise ValueError("snapshot expiry must be after fetched_at")
        return self


class ConfirmedCompetitor(StrictModel):
    competitor_id: CompetitorId
    business_name: str = Field(min_length=1, max_length=240)
    website_url: HttpUrl
    public_gbp_url: HttpUrl | None = None
    confirmation_source: Literal["user", "system"]


class GenerationLimits(StrictModel):
    max_site_urls: int = Field(default=500, ge=1, le=500)
    max_deep_pages: int = Field(default=50, ge=1, le=50)
    max_competitor_pages_each: int = Field(default=10, ge=1, le=10)
    competitor_count: int = Field(default=COMPETITOR_MAX_COUNT, ge=COMPETITOR_MIN_COUNT, le=COMPETITOR_MAX_COUNT)
    max_pagespeed_pages: int = Field(default=5, ge=0, le=5)
    max_review_samples_each: int = Field(default=30, ge=0, le=30)


class AnalyzeRequest(StrictModel):
    case_id: UUID
    report_type: ReportType
    business_identity: BusinessIdentity
    primary_service: str = Field(min_length=1, max_length=200)
    target_market: TargetMarket
    queries: list[str] = Field(min_length=3, max_length=5)
    competitors: list[ConfirmedCompetitor] = Field(
        min_length=COMPETITOR_MIN_COUNT,
        max_length=COMPETITOR_MAX_COUNT,
    )
    first_party_snapshots: list[FirstPartySnapshotEnvelope] = Field(default_factory=list, max_length=3)
    parent_report: ReportV22 | None = None
    generation_limits: GenerationLimits = Field(default_factory=GenerationLimits)

    @model_validator(mode="after")
    def validate_analysis_mode(self) -> "AnalyzeRequest":
        normalized_queries = [query.casefold() for query in self.queries]
        if len(set(normalized_queries)) != len(normalized_queries):
            raise ValueError("queries must be unique after case normalization")
        competitor_ids = [competitor.competitor_id for competitor in self.competitors]
        competitor_domains = [str(competitor.website_url).casefold() for competitor in self.competitors]
        if len(set(competitor_ids)) != len(competitor_ids) or len(set(competitor_domains)) != len(competitor_domains):
            raise ValueError("analyze requires distinct competitors")
        if self.generation_limits.competitor_count != len(self.competitors):
            raise ValueError("generation limit competitor count must match selected competitors")

        source_types = [snapshot.source_type for snapshot in self.first_party_snapshots]
        if len(set(source_types)) != len(source_types):
            raise ValueError("first-party snapshot source types must be unique")
        if self.report_type == "prospect":
            if self.first_party_snapshots or self.parent_report is not None:
                raise ValueError("prospect analysis must not include first-party snapshots or a parent report")
        else:
            if not {"gsc", "ga4"}.issubset(source_types):
                raise ValueError("verified analysis requires GSC and GA4 snapshots")
            if self.parent_report is None:
                raise ValueError("verified analysis requires a parent report")
            if self.parent_report.identity.case_id != self.case_id:
                raise ValueError("parent report must belong to the same case")
        return self


class JobError(StrictModel):
    error_code: str = Field(min_length=1, max_length=120, pattern=r"^[A-Z0-9_]+$")
    user_message: str = Field(min_length=1)
    retryable: bool
    stage: JobStage
    diagnostic_id: UUID


class TaskCreateResponse(StrictModel):
    job_id: UUID
    status: Literal["queued"] = "queued"
    estimated_seconds: int = Field(ge=1, le=3600)


class TaskStatusResponse(StrictModel):
    job_id: UUID
    status: JobStatus
    stage: JobStage
    progress: int = Field(ge=0, le=100)
    message: str = Field(min_length=1)
    report: ReportV22 | None = None
    error: JobError | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime

    @model_validator(mode="after")
    def validate_terminal_payload(self) -> "TaskStatusResponse":
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not precede created_at")
        if self.status == "succeeded":
            if self.report is None or self.error is not None or self.progress != 100 or self.stage != "completed":
                raise ValueError("succeeded jobs require a completed report and no error")
        elif self.status == "failed":
            if self.error is None or self.report is not None or self.stage != "failed":
                raise ValueError("failed jobs require an error and no report")
        elif self.report is not None or self.error is not None:
            raise ValueError("non-terminal jobs must not include report or error payloads")
        return self


class RetryTaskResponse(StrictModel):
    job_id: UUID
    status: Literal["queued"] = "queued"
    attempt_count: int = Field(ge=2)


class ApiV2ContractBundle(StrictModel):
    """Schema-only root that places every endpoint model into one JSON Schema."""

    contract_version: ContractVersion
    preflight_request: PreflightRequest
    preflight_response: PreflightResponse
    analyze_request: AnalyzeRequest
    task_create_response: TaskCreateResponse
    task_status_response: TaskStatusResponse
    retry_task_response: RetryTaskResponse
