"""Internal interfaces only; the frozen Finding and LayerAssessment stay intact."""
from typing import Literal
from uuid import UUID

from pydantic import Field, HttpUrl, model_validator

from app.collectors.site_inventory_models import SitePageType
from app.report_v22.evidence_models import EvidenceBuildInput, EvidenceBuildResult
from app.report_v22.models import CompetitorId, EvidenceId, Finding, FindingId, LayerAssessment, StrictModel

EvaluationState = Literal["triggered", "not_triggered", "not_checked"]
EvaluationReason = Literal[
    "condition_met", "condition_not_met", "source_missing", "source_ineligible",
    "field_not_observed", "insufficient_sample", "identity_unresolved",
    "rank_basis_mismatch", "comparison_time_gap", "ambiguous_page_observations",
    "customer_public_gbp_missing", "semantic_rules_not_implemented",
]


class PublicFindingsLimits(StrictModel):
    max_findings: int = Field(default=10_000, ge=1, le=10_000)
    max_evaluations: int = Field(default=20_000, ge=1, le=20_000)
    max_bytes: int = Field(default=20_000_000, ge=1, le=20_000_000)


class PublicFindingsInput(StrictModel):
    evidence_input: EvidenceBuildInput
    limits: PublicFindingsLimits = Field(default_factory=PublicFindingsLimits)


class RuleTarget(StrictModel):
    kind: Literal["site", "page", "title_group", "market", "page_type", "customer_gbp"]
    snapshot_id: UUID | None = None
    urls: list[HttpUrl] = Field(default_factory=list)
    title_key: str | None = None
    query: str | None = None
    result_type: Literal["maps", "local_pack", "organic"] | None = None
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    country_code: str | None = None
    language: str | None = None
    device: Literal["desktop", "mobile"] | None = None
    page_type: SitePageType | None = None
    competitor_ids: list[CompetitorId] = Field(default_factory=list)

    @model_validator(mode="after")
    def canonical_sets(self):
        self.urls = sorted(set(self.urls), key=str)
        self.competitor_ids = sorted(set(self.competitor_ids))
        return self


class RuleEvaluation(StrictModel):
    rule_id: str
    rule_version: str
    target: RuleTarget
    state: EvaluationState
    reason: EvaluationReason
    evidence_ids: list[EvidenceId] = Field(default_factory=list)
    comparator_ids: list[EvidenceId] = Field(default_factory=list)
    finding_id: FindingId | None = None
    limitations: list[str] = Field(default_factory=list)


class RuleOutcome(StrictModel):
    evaluation: RuleEvaluation
    finding: Finding | None = None


class SamplingCounts(StrictModel):
    discovered: int | None = Field(default=None, ge=0)
    checked: int | None = Field(default=None, ge=0)
    eligible_html: int | None = Field(default=None, ge=0)
    deep_analyzed: int | None = Field(default=None, ge=0)


class FindingsRollup(StrictModel):
    scope: Literal["sampled_site", "sampled_page_type"]
    site_url: HttpUrl
    page_type: SitePageType | None = None
    urls: list[HttpUrl] = Field(default_factory=list)
    counts: SamplingCounts
    finding_ids: list[FindingId]
    evidence_ids: list[EvidenceId]
    layers: list[LayerAssessment]
    limitations: list[str]


class PublicFindingsResult(StrictModel):
    ruleset_version: Literal["v22_public_findings_v1"] = "v22_public_findings_v1"
    evidence_result: EvidenceBuildResult
    findings: list[Finding]
    rule_evaluations: list[RuleEvaluation]
    site_rollup: FindingsRollup
    cluster_rollups: list[FindingsRollup]
