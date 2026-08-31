"""Internal-only evidence inputs and audit output; not part of report_v2_2.

Bindings must come from an authorized snapshot store. This offline module does
not allocate IDs, prove persistence, or authorize tenants.
"""

from datetime import date
import math
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, Field, model_validator

from app.api.v2.models import ConfirmedCompetitor, DimensionValue, FirstPartySnapshotEnvelope, ProviderRequestContext
from app.collectors.serp_market_models import SerpMarketSnapshot
from app.collectors.site_inventory_models import SiteInventorySnapshot
from app.competitors_v22.models import CompetitorCollectionSnapshot, SharedMarketSnapshot
from app.report_v22.models import (
    CaseContext, CompetitorId, Confidence, EvidenceId, EvidenceItem, HealthStatus,
    IdentityMatchStatus, ReportType, ScalarValue, SourceLocator, SourceType, StrictModel,
)
from pydantic import HttpUrl
from app.report_v22.public_gbp_models import CustomerPublicGbpReference, CustomerPublicGbpSnapshot

ActualSourceType = Literal["site", "serp", "competitor", "gsc", "gbp", "ga4"]
GapReason = Literal["no_snapshot", "not_connected", "unavailable", "empty", "partial", "unhealthy", "identity_mismatch", "identity_unconfirmed", "expired"]


def reject_nonfinite(value: object) -> None:
    """Check before JSON encoding: orjson would silently encode NaN as null."""
    if isinstance(value, BaseModel):
        # Invalid copied models are revalidated by the entry point; serializer
        # warnings must not echo their payloads before the safe error is raised.
        reject_nonfinite(value.model_dump(mode="python", warnings=False))
    elif isinstance(value, float) and not math.isfinite(value):
        raise ValueError("nonfinite evidence input")
    elif isinstance(value, dict):
        for item in value.values():
            reject_nonfinite(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            reject_nonfinite(item)


class EvidenceBuildContext(CaseContext):
    case_id: UUID
    report_type: ReportType
    site_url: HttpUrl
    competitors: list[ConfirmedCompetitor] = Field(min_length=3, max_length=3)
    evaluated_at: AwareDatetime
    customer_public_gbp: CustomerPublicGbpReference | None = None

    @model_validator(mode="after")
    def unique_competitors(self):
        if len({c.competitor_id for c in self.competitors}) != 3:
            raise ValueError("duplicate competitor")
        return self


class SnapshotBinding(StrictModel):
    snapshot_id: UUID
    case_id: UUID
    source_type: ActualSourceType
    schema_version: str = Field(min_length=1, max_length=64)
    payload_checksum: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    fetched_at: AwareDatetime
    expires_at: AwareDatetime | None = None
    health_status: HealthStatus = "not_checked"
    identity_match_status: IdentityMatchStatus = "not_checked"

    @model_validator(mode="after")
    def time_order(self):
        if self.expires_at is not None and self.expires_at <= self.fetched_at:
            raise ValueError("invalid snapshot interval")
        return self


class SiteEvidenceSource(StrictModel):
    kind: Literal["site"] = "site"
    binding: SnapshotBinding
    payload: SiteInventorySnapshot


class SerpEvidenceSource(StrictModel):
    kind: Literal["serp"] = "serp"
    binding: SnapshotBinding
    payload: SerpMarketSnapshot
    shared_snapshot: SharedMarketSnapshot | None = None


class CompetitorEvidenceSource(StrictModel):
    kind: Literal["competitor"] = "competitor"
    binding: SnapshotBinding
    payload: CompetitorCollectionSnapshot


class FirstPartyEvidenceSource(StrictModel):
    kind: Literal["first_party"] = "first_party"
    binding: SnapshotBinding
    payload: FirstPartySnapshotEnvelope


class PublicGbpEvidenceSource(StrictModel):
    kind: Literal["public_gbp"] = "public_gbp"
    binding: SnapshotBinding
    payload: CustomerPublicGbpSnapshot


EvidenceSource = Annotated[SiteEvidenceSource | SerpEvidenceSource | CompetitorEvidenceSource | FirstPartyEvidenceSource | PublicGbpEvidenceSource, Field(discriminator="kind")]


class GbpOriginMetadata(StrictModel):
    source_type: ActualSourceType
    gbp_origin: Literal["public_profile", "first_party"] | None = None

    @model_validator(mode="after")
    def origin_requires_gbp(self):
        if self.gbp_origin is not None and self.source_type != "gbp":
            raise ValueError("GBP origin requires GBP source")
        return self


class MissingEvidenceSource(GbpOriginMetadata):
    reason: Literal["no_snapshot", "not_connected", "unavailable"] = "no_snapshot"
    competitor_id: CompetitorId | None = None

    @model_validator(mode="after")
    def public_is_not_authorized_connection(self):
        if self.gbp_origin == "public_profile" and self.reason == "not_connected":
            raise ValueError("public profile has no authorized connection")
        return self


class EvidenceBuildLimits(StrictModel):
    max_items: int = Field(default=50_000, ge=1, le=50_000)
    max_bytes: int = Field(default=20_000_000, ge=1, le=20_000_000)


class EvidenceBuildInput(StrictModel):
    context: EvidenceBuildContext
    sources: list[EvidenceSource] = Field(default_factory=list, max_length=100)
    missing_sources: list[MissingEvidenceSource] = Field(default_factory=list, max_length=100)
    limits: EvidenceBuildLimits = Field(default_factory=EvidenceBuildLimits)

    @model_validator(mode="after")
    def finite(self):
        reject_nonfinite(self)
        return self


class EvidenceSelector(StrictModel):
    category: Literal["site_field", "page_fragment", "serp_field", "competitor_field", "public_gbp_field", "metric", "coverage"]
    record_key: str = Field(min_length=1, max_length=500)
    record_context: list[str | None] = Field(default_factory=list)
    field: str = Field(min_length=1, max_length=120)
    competitor_id: CompetitorId | None = None
    metric_key: str | None = Field(default=None, max_length=120)
    unit: str | None = Field(default=None, max_length=40)
    dimensions: list[DimensionValue] = Field(default_factory=list)
    request_context: ProviderRequestContext | None = None

    @model_validator(mode="after")
    def unique_dimensions(self):
        if len({d.key for d in self.dimensions}) != len(self.dimensions):
            raise ValueError("duplicate dimension key")
        self.dimensions = sorted(self.dimensions, key=lambda d: d.key)
        return self


class EvidenceObservation(StrictModel):
    snapshot_id: UUID
    source_type: SourceType
    selector: EvidenceSelector
    source_locator: SourceLocator
    original_value: ScalarValue
    normalized_value: ScalarValue
    collected_at: AwareDatetime
    coverage_start: date | None = None
    coverage_end: date | None = None
    confidence: Confidence
    health_status: HealthStatus
    limitations: list[str] = Field(default_factory=list)
    origin_paths: list[str] = Field(min_length=1)
    gap_reason: GapReason | None = None

    @model_validator(mode="after")
    def finite(self):
        reject_nonfinite(self)
        return self


class EvidenceSourceTrace(StrictModel):
    evidence_id: EvidenceId
    snapshot_id: UUID
    selector: EvidenceSelector
    origin_paths: list[str] = Field(min_length=1)


class EvidenceSourceSummary(GbpOriginMetadata):
    snapshot_id: UUID
    health_status: HealthStatus
    identity_match_status: IdentityMatchStatus
    business_eligible: bool
    evidence_count: int = Field(ge=0)
    limitations: list[str]


class EvidenceCoverageGap(GbpOriginMetadata):
    reason: GapReason
    snapshot_id: UUID | None = None
    competitor_id: CompetitorId | None = None
    evidence_id: EvidenceId | None = None


class EvidenceBuildResult(StrictModel):
    evidence_index: list[EvidenceItem]
    source_traces: list[EvidenceSourceTrace]
    source_summaries: list[EvidenceSourceSummary]
    coverage_gaps: list[EvidenceCoverageGap]
