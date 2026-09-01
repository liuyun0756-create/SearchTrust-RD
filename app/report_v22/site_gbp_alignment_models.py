"""Strict internal contracts for deterministic site/public GBP alignment."""
from typing import Literal
from uuid import UUID

from pydantic import Field, HttpUrl, model_validator

from app.report_v22.evidence_models import EvidenceSourceTrace
from app.report_v22.models import EvidenceId, EvidenceItem, StrictModel
from app.report_v22.site_business_models import CandidateId, FieldKind

ALIGNMENT_VERSION = "site_gbp_alignment_v1"
SELECTION_VERSION = "site_business_selection_v1"

EligibilityState = Literal["eligible", "unresolved", "excluded"]
EligibilityReason = Literal[
    "core_declared_entity",
    "noncore_name_anchor",
    "noncore_market_anchor",
    "explicit_label_core",
    "repeated_across_final_pages",
    "structured_value_corroboration",
    "ownership_unresolved",
    "external_entity_hint",
    "noncore_anchor_missing",
    "page_not_eligible",
]
FieldState = Literal[
    "exact_match",
    "semantic_match",
    "partial_match",
    "mismatch",
    "site_missing",
    "gbp_missing",
    "both_missing",
    "not_applicable",
    "not_checked",
]
NotCheckedReason = Literal[
    "source_missing",
    "source_ineligible",
    "identity_unresolved",
    "site_content_not_checked",
    "comparison_time_gap",
    "comparator_unsupported",
]
PairState = Literal["exact_match", "semantic_match", "mismatch", "incomparable"]
ServiceAreaState = Literal["exact_match", "partial_match", "mismatch"]


class SiteGbpAlignmentLimits(StrictModel):
    max_output_bytes: int = Field(default=20_000_000, ge=1, le=20_000_000)
    max_candidates: int = Field(default=5_000, ge=1, le=5_000)
    max_values_per_field_per_side: int = Field(default=5_000, ge=1, le=5_000)
    max_pair_comparisons: int = Field(default=250_000, ge=1, le=250_000)
    max_evidence_references: int = Field(default=20_000, ge=1, le=20_000)
    max_coverage_evidence: int = Field(default=100, ge=1, le=100)
    max_merged_evidence: int = Field(default=20_000, ge=1, le=20_000)
    max_urls: int = Field(default=500, ge=1, le=500)
    max_comparison_age_days: int = Field(default=30, ge=1, le=30)


class CandidateEligibility(StrictModel):
    candidate_id: CandidateId
    field: FieldKind
    state: EligibilityState
    reasons: list[EligibilityReason] = Field(min_length=1)
    entity_key: str | None = None
    requested_url: HttpUrl
    final_url: HttpUrl
    evidence_ids: list[EvidenceId] = Field(min_length=1)
    qualification_version: Literal["site_business_selection_v1"] = SELECTION_VERSION

    @model_validator(mode="after")
    def canonical(self):
        self.reasons = sorted(set(self.reasons))
        self.evidence_ids = sorted(set(self.evidence_ids))
        return self


class NormalizedValue(StrictModel):
    field: FieldKind
    original_key: str
    normalized_key: str | None = None
    components: dict[str, str] = Field(default_factory=dict)
    extension: str | None = None
    version: str
    country_code: str
    transformations: list[str] = Field(default_factory=list)
    valid: bool = True

    @model_validator(mode="after")
    def canonical(self):
        self.transformations = sorted(set(self.transformations))
        return self


class ValueComparison(StrictModel):
    state: PairState
    left: NormalizedValue
    right: NormalizedValue
    compared_components: list[str] = Field(default_factory=list)
    omitted_components: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def canonical(self):
        self.compared_components = sorted(set(self.compared_components))
        self.omitted_components = sorted(set(self.omitted_components))
        return self


class ServiceAreaComparison(StrictModel):
    state: ServiceAreaState
    matched: list[str] = Field(default_factory=list)
    site_only: list[str] = Field(default_factory=list)
    gbp_only: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def canonical(self):
        self.matched = sorted(set(self.matched))
        self.site_only = sorted(set(self.site_only))
        self.gbp_only = sorted(set(self.gbp_only))
        return self


class AlignmentPair(StrictModel):
    site_evidence_ids: list[EvidenceId] = Field(min_length=1)
    gbp_evidence_ids: list[EvidenceId] = Field(min_length=1)
    state: PairState
    site_value: NormalizedValue
    gbp_value: NormalizedValue
    compared_components: list[str] = Field(default_factory=list)
    omitted_components: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def canonical(self):
        self.site_evidence_ids = sorted(set(self.site_evidence_ids))
        self.gbp_evidence_ids = sorted(set(self.gbp_evidence_ids))
        self.compared_components = sorted(set(self.compared_components))
        self.omitted_components = sorted(set(self.omitted_components))
        return self


class SiteFieldInspection(StrictModel):
    field: FieldKind
    complete: bool
    required_urls: list[HttpUrl] = Field(default_factory=list)
    checked_urls: list[HttpUrl] = Field(default_factory=list)
    invalid_value_observed: bool = False
    limitations: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def canonical(self):
        self.required_urls = sorted(set(self.required_urls), key=str)
        self.checked_urls = sorted(set(self.checked_urls), key=str)
        self.limitations = sorted(set(self.limitations))
        return self


class SiteBusinessSelectionResult(StrictModel):
    qualification_version: Literal["site_business_selection_v1"] = SELECTION_VERSION
    source_state: Literal["ready", "missing", "ineligible"]
    eligibility: list[CandidateEligibility] = Field(default_factory=list)
    inspections: list[SiteFieldInspection] = Field(min_length=4, max_length=4)

    @model_validator(mode="after")
    def canonical(self):
        if sorted(item.field for item in self.inspections) != ["address", "business_name", "phone", "service_area"]:
            raise ValueError("selection requires four inspections")
        self.eligibility = sorted(self.eligibility, key=lambda item: item.candidate_id)
        self.inspections = sorted(self.inspections, key=lambda item: item.field)
        return self


class SiteGbpFieldResult(StrictModel):
    field: FieldKind
    state: FieldState
    not_checked_reason: NotCheckedReason | None = None
    site_evidence_ids: list[EvidenceId] = Field(default_factory=list)
    gbp_evidence_ids: list[EvidenceId] = Field(default_factory=list)
    coverage_evidence_ids: list[EvidenceId] = Field(default_factory=list)
    matched_pairs: list[AlignmentPair] = Field(default_factory=list)
    unmatched_site_ids: list[EvidenceId] = Field(default_factory=list)
    unmatched_gbp_ids: list[EvidenceId] = Field(default_factory=list)
    incomparable_site_ids: list[EvidenceId] = Field(default_factory=list)
    unresolved_candidate_ids: list[CandidateId] = Field(default_factory=list)
    urls: list[HttpUrl] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def shape_and_canonicalize(self):
        if (self.state == "not_checked") != (self.not_checked_reason is not None):
            raise ValueError("not_checked reason does not match state")
        for name in (
            "site_evidence_ids", "gbp_evidence_ids", "coverage_evidence_ids",
            "unmatched_site_ids", "unmatched_gbp_ids", "incomparable_site_ids",
            "unresolved_candidate_ids", "limitations",
        ):
            setattr(self, name, sorted(set(getattr(self, name))))
        self.urls = sorted(set(self.urls), key=str)
        return self


class SiteGbpAlignmentResult(StrictModel):
    alignment_version: Literal["site_gbp_alignment_v1"] = ALIGNMENT_VERSION
    case_id: UUID
    site_snapshot_id: UUID | None = None
    public_gbp_snapshot_id: UUID | None = None
    site_payload_checksum: str | None = None
    public_gbp_payload_checksum: str | None = None
    identity_summary_checksum: str
    eligibility: list[CandidateEligibility] = Field(default_factory=list)
    inspections: list[SiteFieldInspection] = Field(default_factory=list)
    fields: list[SiteGbpFieldResult] = Field(min_length=4, max_length=4)

    @model_validator(mode="after")
    def unique_fields(self):
        if sorted(item.field for item in self.fields) != ["address", "business_name", "phone", "service_area"]:
            raise ValueError("alignment requires four unique fields")
        self.fields = sorted(self.fields, key=lambda item: item.field)
        self.eligibility = sorted(self.eligibility, key=lambda item: item.candidate_id)
        self.inspections = sorted(self.inspections, key=lambda item: item.field)
        return self


class SiteGbpAlignmentBuildResult(StrictModel):
    alignment: SiteGbpAlignmentResult
    evidence_index: list[EvidenceItem] = Field(default_factory=list)
    source_traces: list[EvidenceSourceTrace] = Field(default_factory=list)
