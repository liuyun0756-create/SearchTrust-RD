"""Strict internal API contracts for v2.2 competitor discovery tasks."""

from __future__ import annotations

from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import AwareDatetime, Field, HttpUrl, model_validator

from app.api.v2.models import CompetitorCandidate, DataGap
from app.report_v22.models import BusinessIdentity, StrictModel, TargetMarket


CompetitorDiscoveryStatus = Literal["queued", "running", "succeeded", "failed"]
CompetitorDiscoveryLimitation = Annotated[str, Field(min_length=1, max_length=300)]
CompetitorDiscoveryStage = Literal[
    "queued",
    "collecting_market",
    "ranking_candidates",
    "validating_supplements",
    "completed",
    "failed",
]


class CompetitorDiscoveryRequest(StrictModel):
    case_id: UUID
    business_identity: BusinessIdentity
    primary_service: str = Field(min_length=1, max_length=200)
    target_market: TargetMarket
    queries: list[str] = Field(min_length=3, max_length=5)
    search_language: str = Field(default="en", min_length=2, max_length=12, pattern=r"^[A-Za-z0-9-]+$")
    search_device: Literal["desktop", "mobile"] = "mobile"
    supplemental_website_urls: list[HttpUrl] = Field(default_factory=list, max_length=3)

    @model_validator(mode="after")
    def validate_unique_inputs(self) -> "CompetitorDiscoveryRequest":
        normalized_queries = [query.casefold() for query in self.queries]
        if len(set(normalized_queries)) != len(normalized_queries):
            raise ValueError("queries must be unique after case normalization")
        supplemental_domains = [
            (urlsplit(str(url)).hostname or "").casefold().removeprefix("www.")
            for url in self.supplemental_website_urls
        ]
        if len(set(supplemental_domains)) != len(supplemental_domains):
            raise ValueError("supplemental competitor domains must be unique")
        return self


class CompetitorDiscoveryError(StrictModel):
    error_code: str = Field(min_length=1, max_length=120, pattern=r"^[A-Z0-9_]+$")
    user_message: str = Field(min_length=1, max_length=500)
    retryable: bool
    stage: CompetitorDiscoveryStage
    diagnostic_id: UUID


class CompetitorDiscoveryResult(StrictModel):
    discovery_id: UUID
    case_id: UUID
    input_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    candidate_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    market_snapshot_id: UUID
    market_snapshot_checksum: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    candidates: list[CompetitorCandidate] = Field(default_factory=list, max_length=6)
    ready_for_confirmation: bool
    data_gaps: list[DataGap] = Field(default_factory=list, max_length=50)
    limitations: list[CompetitorDiscoveryLimitation] = Field(default_factory=list, max_length=100)
    created_at: AwareDatetime
    expires_at: AwareDatetime

    @model_validator(mode="after")
    def validate_discovery_result(self) -> "CompetitorDiscoveryResult":
        if self.expires_at <= self.created_at:
            raise ValueError("discovery expiry must follow creation")
        ids = [candidate.competitor_id for candidate in self.candidates]
        domains = [
            (urlsplit(str(candidate.website_url)).hostname or "").casefold().removeprefix("www.")
            for candidate in self.candidates
        ]
        if len(set(ids)) != len(ids):
            raise ValueError("candidate IDs must be unique")
        if len(set(domains)) != len(domains):
            raise ValueError("candidate websites must be unique")
        if self.ready_for_confirmation and len(self.candidates) < 3:
            raise ValueError("ready discovery results require at least three candidates")
        if not self.ready_for_confirmation and not any(gap.blocking for gap in self.data_gaps):
            raise ValueError("unready discovery results require a blocking gap")
        return self


class CompetitorDiscoveryTaskCreateResponse(StrictModel):
    discovery_job_id: UUID
    status: Literal["queued"] = "queued"
    estimated_seconds: int = Field(ge=1, le=1800)


class CompetitorDiscoveryStatusResponse(StrictModel):
    discovery_job_id: UUID
    status: CompetitorDiscoveryStatus
    stage: CompetitorDiscoveryStage
    progress: int = Field(ge=0, le=100)
    message: str = Field(min_length=1, max_length=500)
    result: CompetitorDiscoveryResult | None = None
    error: CompetitorDiscoveryError | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime

    @model_validator(mode="after")
    def validate_lifecycle(self) -> "CompetitorDiscoveryStatusResponse":
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not precede created_at")
        if self.status == "succeeded":
            if self.result is None or self.error is not None or self.stage != "completed" or self.progress != 100:
                raise ValueError("succeeded discovery jobs require a completed result")
        elif self.status == "failed":
            if self.error is None or self.result is not None or self.stage != "failed":
                raise ValueError("failed discovery jobs require an error")
        elif self.result is not None or self.error is not None:
            raise ValueError("non-terminal discovery jobs cannot contain terminal payloads")
        return self


class CompetitorDiscoveryRetryResponse(StrictModel):
    discovery_job_id: UUID
    status: Literal["queued"] = "queued"
    attempt_count: int = Field(ge=2)
