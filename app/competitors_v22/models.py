"""Strict internal contracts for v2.2 competitor selection and collection."""

from __future__ import annotations

from math import isclose
from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import AwareDatetime, Field, HttpUrl, model_validator

from app.api.v2.competitor_models import (
    CompetitorDiscoveryError,
    CompetitorDiscoveryResult,
    CompetitorDiscoveryStage,
    CompetitorDiscoveryStatus,
)
from app.api.v2.models import CompetitorCandidate, ConfirmedCompetitor, DataGap
from app.collectors.serp_market_models import SerpMarketSnapshot
from app.collectors.site_inventory_models import SiteInventorySnapshot
from app.competitors_v22.limits import (
    COMPETITOR_COUNT,
    COMPETITOR_DISCOVERY_CANDIDATE_LIMIT,
    COMPETITOR_DISCOVERY_SUPPLEMENTAL_LIMIT,
    COMPETITOR_MAX_COUNT,
    COMPETITOR_MIN_COUNT,
    COMPETITOR_PROVIDER_ATTEMPT_LIMIT,
    COMPETITOR_REVIEW_PAGE_LIMIT,
    COMPETITOR_REVIEW_PAGE_SIZE,
    COMPETITOR_REVIEW_SAMPLE_LIMIT,
    COMPETITOR_SITE_DEEP_LIMIT,
    COMPETITOR_SITE_DISCOVERY_LIMIT,
)
from app.report_v22.models import StrictModel


CandidateDisposition = Literal["eligible", "excluded", "ambiguous"]
CompetitorSourceStatus = Literal["available", "partial", "unavailable"]
BoundedIdentitySignal = Annotated[str, Field(min_length=1, max_length=500)]
BoundedLimitation = Annotated[str, Field(min_length=1, max_length=300)]
BoundedCategory = Annotated[str, Field(min_length=1, max_length=240)]


class CompetitorDiscoveryJobState(StrictModel):
    discovery_job_id: UUID
    case_id: UUID
    status: CompetitorDiscoveryStatus
    stage: CompetitorDiscoveryStage
    progress: int = Field(ge=0, le=100)
    message: str = Field(min_length=1, max_length=500)
    attempt_count: int = Field(ge=0)
    run_generation: int = Field(ge=1)
    revision: int = Field(ge=1)
    request_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    idempotency_key_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    heartbeat_at: AwareDatetime | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime
    completed_at: AwareDatetime | None = None
    result: CompetitorDiscoveryResult | None = None
    error: CompetitorDiscoveryError | None = None

    @model_validator(mode="after")
    def validate_lifecycle(self) -> "CompetitorDiscoveryJobState":
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not precede created_at")
        if self.completed_at is not None and self.completed_at < self.created_at:
            raise ValueError("completed_at must not precede created_at")
        if self.status == "succeeded":
            if (
                self.result is None
                or self.error is not None
                or self.stage != "completed"
                or self.progress != 100
                or self.completed_at is None
            ):
                raise ValueError("succeeded discovery jobs require a completed result")
            if self.result.discovery_id != self.discovery_job_id or self.result.case_id != self.case_id:
                raise ValueError("discovery result identity must match the job")
        elif self.status == "failed":
            if (
                self.error is None
                or self.result is not None
                or self.stage != "failed"
                or self.completed_at is None
            ):
                raise ValueError("failed discovery jobs require an error")
        elif self.result is not None or self.error is not None or self.completed_at is not None:
            raise ValueError("non-terminal discovery jobs cannot contain terminal payloads")
        return self

    @property
    def terminal(self) -> bool:
        return self.status in {"succeeded", "failed"}


class CandidateScore(StrictModel):
    query_coverage: float = Field(ge=0, le=1)
    rank: float = Field(ge=0, le=1)
    business_relevance: float = Field(ge=0, le=1)
    identity_confidence: float = Field(ge=0, le=1)
    total: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def validate_total(self) -> "CandidateScore":
        expected = (
            self.query_coverage * 0.45
            + self.rank * 0.30
            + self.business_relevance * 0.20
            + self.identity_confidence * 0.05
        )
        if not isclose(self.total, expected, abs_tol=1e-6):
            raise ValueError("total must match the weighted candidate formula")
        return self


class CandidateIdentityAudit(StrictModel):
    stable_identity_key: str = Field(min_length=1, max_length=600)
    normalized_name: str = Field(min_length=1, max_length=240)
    normalized_domain: str | None = Field(default=None, max_length=253)
    normalized_address: str | None = Field(default=None, max_length=500)
    provider_place_ids: list[BoundedIdentitySignal] = Field(default_factory=list, max_length=20)
    provider_data_ids: list[BoundedIdentitySignal] = Field(default_factory=list, max_length=20)
    provider_cids: list[BoundedIdentitySignal] = Field(default_factory=list, max_length=20)
    result_record_ids: list[BoundedIdentitySignal] = Field(min_length=1, max_length=300)
    queries: list[Annotated[str, Field(min_length=1, max_length=300)]] = Field(min_length=1, max_length=5)
    result_types: list[Literal["maps", "local_pack", "organic"]] = Field(min_length=1, max_length=3)

    @model_validator(mode="after")
    def validate_unique_signals(self) -> "CandidateIdentityAudit":
        for values in (
            self.provider_place_ids,
            self.provider_data_ids,
            self.provider_cids,
            self.result_record_ids,
            self.queries,
            self.result_types,
        ):
            if len(set(values)) != len(values):
                raise ValueError("candidate identity signals must be unique")
        return self


class CandidateAuditRecord(StrictModel):
    identity: CandidateIdentityAudit
    disposition: CandidateDisposition
    reason_code: str = Field(min_length=1, max_length=120, pattern=r"^[a-z0-9_]+$")
    score: CandidateScore | None = None
    candidate: CompetitorCandidate | None = None

    @model_validator(mode="after")
    def validate_disposition(self) -> "CandidateAuditRecord":
        if self.disposition == "eligible":
            if self.score is None or self.candidate is None:
                raise ValueError("eligible audit records require a score and candidate")
        elif self.candidate is not None:
            raise ValueError("ineligible audit records cannot expose a candidate")
        return self


class CandidateRankingResult(StrictModel):
    candidates: list[CompetitorCandidate] = Field(
        default_factory=list,
        max_length=COMPETITOR_DISCOVERY_CANDIDATE_LIMIT,
    )
    audit_records: list[CandidateAuditRecord] = Field(default_factory=list, max_length=300)
    ready_for_confirmation: bool
    data_gaps: list[DataGap] = Field(default_factory=list, max_length=50)
    limitations: list[BoundedLimitation] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def validate_ranking(self) -> "CandidateRankingResult":
        ids = [candidate.competitor_id for candidate in self.candidates]
        domains = [
            (urlsplit(str(candidate.website_url)).hostname or "").casefold().removeprefix("www.")
            for candidate in self.candidates
        ]
        if len(set(ids)) != len(ids) or len(set(domains)) != len(domains):
            raise ValueError("ranked candidates must have unique IDs and websites")
        if self.ready_for_confirmation and len(self.candidates) < COMPETITOR_MIN_COUNT:
            raise ValueError("ready rankings require at least one candidate")
        if not self.ready_for_confirmation and not any(gap.blocking for gap in self.data_gaps):
            raise ValueError("unready rankings require a blocking gap")
        return self


class SharedMarketSnapshot(StrictModel):
    schema_version: Literal["competitor_shared_market_v1"]
    snapshot_id: UUID
    source_job_id: UUID
    input_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    snapshot_checksum: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    created_at: AwareDatetime
    expires_at: AwareDatetime
    snapshot: SerpMarketSnapshot

    @model_validator(mode="after")
    def validate_shared_snapshot(self) -> "SharedMarketSnapshot":
        if self.expires_at <= self.created_at:
            raise ValueError("shared market expiry must follow creation")
        if self.snapshot.job_id != self.source_job_id:
            raise ValueError("shared market source job must match the snapshot")
        return self


class PublicReviewRecord(StrictModel):
    review_record_id: str = Field(pattern=r"^rv_[a-z0-9]{16,80}$")
    provider_review_id: str | None = Field(default=None, max_length=500)
    rating: float = Field(ge=0, le=5)
    iso_date: AwareDatetime | None = None
    original_date_text: str | None = Field(default=None, max_length=120)
    text: str | None = Field(default=None, max_length=10_000)
    owner_response_text: str | None = Field(default=None, max_length=10_000)
    owner_response_iso_date: AwareDatetime | None = None
    source: Literal["google_maps_reviews"]
    collected_at: AwareDatetime
    request_record_id: str = Field(pattern=r"^req_[a-z0-9]{16,80}$")

    @model_validator(mode="after")
    def validate_owner_response(self) -> "PublicReviewRecord":
        if self.owner_response_iso_date is not None and self.owner_response_text is None:
            raise ValueError("owner response date requires response text")
        return self


class PublicGbpProfile(StrictModel):
    business_name: str = Field(min_length=1, max_length=240)
    public_gbp_url: HttpUrl | None = None
    provider_place_id: str | None = Field(default=None, max_length=500)
    provider_data_id: str | None = Field(default=None, max_length=500)
    provider_cid: str | None = Field(default=None, max_length=200)
    website_url: HttpUrl | None = None
    address: str | None = Field(default=None, max_length=500)
    categories: list[BoundedCategory] = Field(default_factory=list, max_length=20)
    rating: float | None = Field(default=None, ge=0, le=5)
    review_count: int | None = Field(default=None, ge=0, le=1_000_000_000)
    collected_at: AwareDatetime
    request_record_id: str | None = Field(default=None, pattern=r"^req_[a-z0-9]{16,80}$")

    @model_validator(mode="after")
    def validate_categories(self) -> "PublicGbpProfile":
        if len(set(self.categories)) != len(self.categories):
            raise ValueError("public GBP categories must be unique")
        return self


class CompetitorSnapshot(StrictModel):
    competitor: ConfirmedCompetitor
    query_appearance_count: int = Field(ge=1, le=5)
    best_position: int = Field(ge=1, le=10_000)
    analyzed_page_count: int = Field(ge=0, le=COMPETITOR_SITE_DEEP_LIMIT)
    site_status: CompetitorSourceStatus
    public_gbp_status: CompetitorSourceStatus
    reviews_status: CompetitorSourceStatus
    site_inventory: SiteInventorySnapshot | None = None
    public_gbp: PublicGbpProfile | None = None
    reviews: list[PublicReviewRecord] = Field(default_factory=list, max_length=COMPETITOR_REVIEW_SAMPLE_LIMIT)
    limitations: list[BoundedLimitation] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def validate_sources(self) -> "CompetitorSnapshot":
        if self.site_inventory is None:
            if self.analyzed_page_count != 0 or self.site_status == "available":
                raise ValueError("missing site inventory cannot be available or analyzed")
        else:
            if self.site_status == "unavailable":
                raise ValueError("site inventory cannot be marked unavailable")
            if self.site_inventory.deep_analyzed_count != self.analyzed_page_count:
                raise ValueError("analyzed page count must match site inventory")
            host = (urlsplit(str(self.competitor.website_url)).hostname or "").casefold().removeprefix("www.")
            if self.site_inventory.canonical_host != host:
                raise ValueError("site inventory must belong to the competitor website")
        if self.public_gbp is None and self.public_gbp_status == "available":
            raise ValueError("available public GBP status requires a profile")
        if not self.reviews and self.reviews_status == "available":
            raise ValueError("available review status requires samples")
        return self


class CompetitorCollectionBudget(StrictModel):
    site_discovery_limit_each: int = Field(ge=1, le=COMPETITOR_SITE_DISCOVERY_LIMIT)
    site_deep_limit_each: int = Field(ge=1, le=COMPETITOR_SITE_DEEP_LIMIT)
    review_sample_limit_each: int = Field(ge=0, le=COMPETITOR_REVIEW_SAMPLE_LIMIT)
    provider_attempt_limit: int = Field(ge=0, le=COMPETITOR_PROVIDER_ATTEMPT_LIMIT)
    provider_attempts_used: int = Field(ge=0, le=COMPETITOR_PROVIDER_ATTEMPT_LIMIT)
    place_detail_calls: int = Field(ge=0, le=COMPETITOR_MAX_COUNT)
    review_page_calls: int = Field(ge=0, le=COMPETITOR_MAX_COUNT * COMPETITOR_REVIEW_PAGE_LIMIT)
    checkpoint_hits: int = Field(ge=0)
    truncated: bool

    @model_validator(mode="after")
    def validate_budget(self) -> "CompetitorCollectionBudget":
        if self.provider_attempts_used > self.provider_attempt_limit:
            raise ValueError("provider attempts cannot exceed the hard limit")
        if self.provider_attempts_used < self.place_detail_calls + self.review_page_calls:
            raise ValueError("provider attempts must cover completed provider calls")
        return self


class CompetitorCollectionSnapshot(StrictModel):
    schema_version: Literal["competitor_collection_snapshot_v1"]
    job_id: UUID
    discovery_id: UUID
    candidate_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    market_snapshot_id: UUID
    market_snapshot_checksum: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    started_at: AwareDatetime
    completed_at: AwareDatetime
    competitors: list[CompetitorSnapshot] = Field(
        min_length=COMPETITOR_MIN_COUNT,
        max_length=COMPETITOR_MAX_COUNT,
    )
    budget: CompetitorCollectionBudget
    limitations: list[BoundedLimitation] = Field(default_factory=list, max_length=200)

    @model_validator(mode="after")
    def validate_collection(self) -> "CompetitorCollectionSnapshot":
        if self.completed_at < self.started_at:
            raise ValueError("competitor collection completion cannot precede start")
        ids = [item.competitor.competitor_id for item in self.competitors]
        domains = [
            (urlsplit(str(item.competitor.website_url)).hostname or "").casefold().removeprefix("www.")
            for item in self.competitors
        ]
        if len(set(ids)) != len(ids):
            raise ValueError("competitor IDs must be unique")
        if len(set(domains)) != len(domains):
            raise ValueError("competitor websites must be unique")
        return self
