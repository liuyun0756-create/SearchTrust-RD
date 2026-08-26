"""Strict Pydantic source of truth for the SearchTrust report_v2_2 contract."""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal, Union
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    model_validator,
)

from app.report_v22.contract_version import ContractVersion


EvidenceId = Annotated[str, Field(pattern=r"^ev_[a-z0-9][a-z0-9_-]{2,80}$")]
FindingId = Annotated[str, Field(pattern=r"^fn_[a-z0-9][a-z0-9_-]{2,80}$")]
ActionId = Annotated[str, Field(pattern=r"^ac_[a-z0-9][a-z0-9_-]{2,80}$")]
CompetitorId = Annotated[str, Field(pattern=r"^cp_[a-z0-9][a-z0-9_-]{2,80}$")]
ScalarValue = Union[str, int, float, bool, None]

ReportType = Literal["prospect", "verified_execution"]
OperatingModel = Literal["storefront", "service_area", "hybrid"]
Confidence = Literal["low", "medium", "high"]
Severity = Literal["low", "medium", "high", "critical"]
HealthStatus = Literal[
    "not_checked",
    "healthy",
    "unhealthy",
    "unavailable",
    "expired",
    "error",
]
IdentityMatchStatus = Literal["not_checked", "matched", "mismatch", "needs_confirmation"]
ConnectionState = Literal[
    "not_connected",
    "connected",
    "available",
    "healthy",
    "verified",
    "error",
]
SourceType = Literal[
    "site",
    "serp",
    "competitor",
    "gsc",
    "gbp",
    "ga4",
    "pagespeed",
    "coverage",
]
LayerKey = Literal[
    "foundation",
    "entity_presence",
    "entity_consistency",
    "specificity",
    "real_world_connection",
    "accountability",
    "page_unique_value",
    "algorithm_fit",
]
LayerStatus = Literal["good", "medium", "weak", "not_checked"]
FactClassification = Literal["fact", "inference", "estimate"]
EffortBucket = Literal["small", "medium", "large"]
VersionChangeType = Literal["confirmed", "reprioritized", "refined", "replaced", "new"]

REQUIRED_LAYER_KEYS: tuple[str, ...] = (
    "foundation",
    "entity_presence",
    "entity_consistency",
    "specificity",
    "real_world_connection",
    "accountability",
    "page_unique_value",
    "algorithm_fit",
)


class StrictModel(BaseModel):
    """Base model that rejects coercion and unknown fields."""

    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)


def _duplicates(values: list[str]) -> set[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    return duplicates


class TargetMarket(StrictModel):
    display_name: str = Field(min_length=1, max_length=200)
    country_code: str = Field(min_length=2, max_length=2, pattern=r"^[A-Z]{2}$")
    region: str | None = Field(default=None, max_length=120)
    city: str | None = Field(default=None, max_length=120)
    postal_code: str | None = Field(default=None, max_length=32)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)


class BusinessIdentity(StrictModel):
    business_name: str = Field(min_length=1, max_length=240)
    site_url: HttpUrl
    normalized_domain: str = Field(min_length=1, max_length=253)
    operating_model: OperatingModel
    primary_location: TargetMarket
    public_gbp_url: HttpUrl | None = None


class IdentitySection(StrictModel):
    case_id: UUID
    business: BusinessIdentity


class CaseContext(StrictModel):
    primary_service: str = Field(min_length=1, max_length=200)
    target_market: TargetMarket
    queries: list[str] = Field(min_length=3, max_length=5)
    search_language: str = Field(default="en", min_length=2, max_length=12)
    search_device: Literal["desktop", "mobile"] = "mobile"

    @model_validator(mode="after")
    def validate_queries(self) -> "CaseContext":
        normalized = [query.casefold() for query in self.queries]
        if _duplicates(normalized):
            raise ValueError("queries must be unique after case normalization")
        if any(not query.strip() for query in self.queries):
            raise ValueError("queries must not be blank")
        return self


class ReportVersion(StrictModel):
    schema_version: ContractVersion
    report_id: UUID
    report_type: ReportType
    version_number: int = Field(ge=1)
    parent_report_id: UUID | None = None
    generated_at: AwareDatetime
    ruleset_version: str = Field(min_length=1, max_length=64)
    copy_model_version: str = Field(min_length=1, max_length=120)


class SourceLocator(StrictModel):
    url: HttpUrl | None = None
    external_resource_id: str | None = Field(default=None, max_length=500)
    field_path: str | None = Field(default=None, max_length=500)
    page_path: str | None = Field(default=None, max_length=500)
    query: str | None = Field(default=None, max_length=300)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    device: Literal["desktop", "mobile"] | None = None
    language: str | None = Field(default=None, max_length=12)


class EvidenceItem(StrictModel):
    evidence_id: EvidenceId
    snapshot_id: UUID
    source_type: SourceType
    source_locator: SourceLocator
    original_value: ScalarValue
    normalized_value: ScalarValue
    collected_at: AwareDatetime
    coverage_start: date | None = None
    coverage_end: date | None = None
    confidence: Confidence
    health_status: HealthStatus
    limitations: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def validate_coverage_dates(self) -> "EvidenceItem":
        if self.coverage_start and self.coverage_end and self.coverage_start > self.coverage_end:
            raise ValueError("coverage_start must not be after coverage_end")
        return self


class SourceCoverage(StrictModel):
    source_type: SourceType
    health_status: HealthStatus
    identity_match_status: IdentityMatchStatus
    snapshot_ids: list[UUID] = Field(default_factory=list)
    checked_items: int = Field(default=0, ge=0)
    available_items: int = Field(default=0, ge=0)
    coverage_summary: str = Field(min_length=1)
    limitations: list[str] = Field(default_factory=list)


class DataCoverage(StrictModel):
    full_evidence_coverage: bool
    sources: list[SourceCoverage] = Field(min_length=1)
    limitations: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_sources(self) -> "DataCoverage":
        source_types = [source.source_type for source in self.sources]
        if _duplicates(source_types):
            raise ValueError("data coverage source types must be unique")
        return self


class SearchResult(StrictModel):
    query: str = Field(min_length=1, max_length=300)
    result_type: Literal["maps", "local_pack", "organic"]
    position: int = Field(ge=1)
    business_name: str = Field(min_length=1, max_length=240)
    url: HttpUrl | None = None
    evidence_id: EvidenceId


class MarketSnapshot(StrictModel):
    observed_at: AwareDatetime
    target_market: TargetMarket
    queries: list[str] = Field(min_length=3, max_length=5)
    results: list[SearchResult] = Field(default_factory=list)
    summary: str = Field(min_length=1)
    limitations: list[str] = Field(default_factory=list)


class SitePageSummary(StrictModel):
    url: HttpUrl
    page_type: str = Field(min_length=1, max_length=100)
    crawl_depth: int = Field(ge=0)
    deep_analyzed: bool
    evidence_ids: list[EvidenceId] = Field(default_factory=list)


class LabelCount(StrictModel):
    label: str = Field(min_length=1, max_length=100)
    count: int = Field(ge=0)


class SiteInventorySummary(StrictModel):
    discovered_url_count: int = Field(ge=0, le=500)
    structurally_checked_count: int = Field(ge=0, le=500)
    deep_analyzed_count: int = Field(ge=0, le=50)
    discovery_limit: int = Field(default=500, ge=1, le=500)
    deep_analysis_limit: int = Field(default=50, ge=1, le=50)
    page_type_counts: list[LabelCount] = Field(default_factory=list)
    selected_pages: list[SitePageSummary] = Field(default_factory=list, max_length=50)
    limitations: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_counts(self) -> "SiteInventorySummary":
        if self.structurally_checked_count > self.discovered_url_count:
            raise ValueError("structurally checked count cannot exceed discovered count")
        if self.deep_analyzed_count > self.structurally_checked_count:
            raise ValueError("deep analyzed count cannot exceed structurally checked count")
        if self.deep_analyzed_count != sum(page.deep_analyzed for page in self.selected_pages):
            raise ValueError("deep analyzed count must match selected page entries")
        return self


class CompetitorSummary(StrictModel):
    competitor_id: CompetitorId
    business_name: str = Field(min_length=1, max_length=240)
    website_url: HttpUrl
    public_gbp_url: HttpUrl | None = None
    query_appearance_count: int = Field(ge=1)
    best_position: int = Field(ge=1)
    analyzed_page_count: int = Field(ge=0, le=10)
    strengths: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    evidence_ids: list[EvidenceId] = Field(min_length=1)


class CompetitorAnalysis(StrictModel):
    selection_method: Literal["system_ranked_user_confirmed", "system_ranked"]
    competitors: list[CompetitorSummary] = Field(min_length=3, max_length=3)
    comparison_summary: str = Field(min_length=1)
    limitations: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_competitors(self) -> "CompetitorAnalysis":
        ids = [competitor.competitor_id for competitor in self.competitors]
        domains = [str(competitor.website_url).casefold() for competitor in self.competitors]
        if _duplicates(ids):
            raise ValueError("competitor IDs must be unique")
        if _duplicates(domains):
            raise ValueError("competitor websites must be unique")
        return self


class MetricValue(StrictModel):
    metric_key: str = Field(min_length=1, max_length=120)
    label: str = Field(min_length=1, max_length=160)
    value: float
    unit: str = Field(min_length=1, max_length=40)
    comparison_value: float | None = None


class SourcePerformanceEnvelope(StrictModel):
    source_type: Literal["gsc", "gbp", "ga4"]
    connection_state: ConnectionState
    snapshot_id: UUID | None = None
    identity_match_status: IdentityMatchStatus
    health_status: HealthStatus
    coverage_start: date | None = None
    coverage_end: date | None = None
    metrics: list[MetricValue] = Field(default_factory=list)
    health_reasons: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_coverage_dates(self) -> "SourcePerformanceEnvelope":
        if self.coverage_start and self.coverage_end and self.coverage_start > self.coverage_end:
            raise ValueError("coverage_start must not be after coverage_end")
        return self


class GSCPerformance(SourcePerformanceEnvelope):
    source_type: Literal["gsc"] = "gsc"


class GBPPerformance(SourcePerformanceEnvelope):
    source_type: Literal["gbp"] = "gbp"


class GA4Performance(SourcePerformanceEnvelope):
    source_type: Literal["ga4"] = "ga4"


class FirstPartyPerformance(StrictModel):
    gsc: GSCPerformance
    gbp: GBPPerformance
    ga4: GA4Performance


class Finding(StrictModel):
    finding_id: FindingId
    statement: str = Field(min_length=1)
    evidence_ids: list[EvidenceId] = Field(min_length=1)
    comparator_ids: list[EvidenceId] = Field(default_factory=list)
    rule_id: str = Field(min_length=1, max_length=120)
    rule_version: str = Field(min_length=1, max_length=64)
    classification: FactClassification
    severity: Severity
    scope: str = Field(min_length=1, max_length=240)
    confidence: Confidence
    affected_urls: list[HttpUrl] = Field(default_factory=list)
    affected_queries: list[str] = Field(default_factory=list)
    missing_data: list[str] = Field(default_factory=list)
    change_conditions: list[str] = Field(default_factory=list)


class ExecutiveDecision(StrictModel):
    core_problem: str = Field(min_length=1)
    finding_ids: list[FindingId] = Field(min_length=1)
    why_now: str = Field(min_length=1)
    decision_summary: str = Field(min_length=1)


class LayerAssessment(StrictModel):
    layer_key: LayerKey
    status: LayerStatus
    summary: str = Field(min_length=1)
    finding_ids: list[FindingId] = Field(default_factory=list)
    evidence_ids: list[EvidenceId] = Field(default_factory=list)


class ImplementationStep(StrictModel):
    sequence: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=200)
    instruction: str = Field(min_length=1)


class ActionSpecification(StrictModel):
    content_requirements: list[str] = Field(default_factory=list)
    gbp_requirements: list[str] = Field(default_factory=list)
    technical_requirements: list[str] = Field(default_factory=list)


class ValidationMetric(StrictModel):
    metric_key: str = Field(min_length=1, max_length=120)
    baseline: str = Field(min_length=1, max_length=240)
    success_condition: str = Field(min_length=1)
    source_type: SourceType


class TopAction(StrictModel):
    action_id: ActionId
    sequence: int = Field(ge=1, le=3)
    finding_ids: list[FindingId] = Field(min_length=1)
    why_now: str = Field(min_length=1)
    exact_targets: list[str] = Field(min_length=1)
    implementation_steps: list[ImplementationStep] = Field(min_length=1)
    specification: ActionSpecification
    required_client_assets: list[str] = Field(default_factory=list)
    dependencies: list[ActionId] = Field(default_factory=list)
    owner_suggestion: str = Field(min_length=1, max_length=160)
    effort_bucket: EffortBucket
    definition_of_done: list[str] = Field(min_length=1)
    validation_metrics: list[ValidationMetric] = Field(min_length=1)
    data_sources: list[SourceType] = Field(min_length=1)
    review_date: date
    client_facing_explanation: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_steps(self) -> "TopAction":
        sequences = [step.sequence for step in self.implementation_steps]
        if sequences != list(range(1, len(sequences) + 1)):
            raise ValueError("implementation step sequences must be contiguous and ordered")
        return self


class RoadmapPhase(StrictModel):
    period: Literal["days_1_30", "days_31_60", "days_61_90"]
    objective: str = Field(min_length=1)
    action_ids: list[ActionId] = Field(min_length=1)
    exit_criteria: list[str] = Field(min_length=1)


class Roadmap(StrictModel):
    phases: list[RoadmapPhase] = Field(min_length=3, max_length=3)

    @model_validator(mode="after")
    def validate_periods(self) -> "Roadmap":
        expected = ["days_1_30", "days_31_60", "days_61_90"]
        if [phase.period for phase in self.phases] != expected:
            raise ValueError("roadmap phases must be ordered 30, 60, then 90 days")
        return self


class ClientSummary(StrictModel):
    headline: str = Field(min_length=1)
    core_problem: str = Field(min_length=1)
    opportunity: str = Field(min_length=1)
    action_ids: list[ActionId] = Field(min_length=3, max_length=3)
    required_client_assets: list[str] = Field(default_factory=list)
    next_review_date: date


class PreviousFindingReference(StrictModel):
    report_id: UUID
    finding_id: FindingId
    statement: str = Field(min_length=1)
    fingerprint: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")


class VersionDiffEntry(StrictModel):
    change_type: VersionChangeType
    previous_finding: PreviousFindingReference | None = None
    current_finding_ids: list[FindingId] = Field(min_length=1)
    evidence_ids: list[EvidenceId] = Field(min_length=1)
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_previous_finding(self) -> "VersionDiffEntry":
        if self.change_type == "new" and self.previous_finding is not None:
            raise ValueError("new version changes must not reference a previous finding")
        if self.change_type != "new" and self.previous_finding is None:
            raise ValueError("non-new version changes must reference a previous finding")
        return self


class VersionDiff(StrictModel):
    kind: Literal["initial", "upgrade"]
    parent_report_id: UUID | None = None
    entries: list[VersionDiffEntry] = Field(default_factory=list)


class Limitation(StrictModel):
    limitation_id: str = Field(pattern=r"^lim_[a-z0-9][a-z0-9_-]{2,80}$")
    category: Literal["coverage", "provider", "policy", "confidence", "identity", "other"]
    severity: Severity
    description: str = Field(min_length=1)
    affected_sections: list[str] = Field(min_length=1)


class ReportV22(StrictModel):
    identity: IdentitySection
    case_context: CaseContext
    report_version: ReportVersion
    data_coverage: DataCoverage
    market_snapshot: MarketSnapshot
    site_inventory_summary: SiteInventorySummary
    competitor_analysis: CompetitorAnalysis
    first_party_performance: FirstPartyPerformance
    executive_decision: ExecutiveDecision
    eight_layers: list[LayerAssessment] = Field(min_length=8, max_length=8)
    findings: list[Finding] = Field(min_length=1)
    top_actions: list[TopAction] = Field(min_length=3, max_length=3)
    roadmap_30_60_90: Roadmap
    client_summary: ClientSummary
    evidence_index: list[EvidenceItem] = Field(min_length=1)
    version_diff: VersionDiff
    limitations: list[Limitation] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_contract_invariants(self) -> "ReportV22":
        evidence_ids = [evidence.evidence_id for evidence in self.evidence_index]
        finding_ids = [finding.finding_id for finding in self.findings]
        action_ids = [action.action_id for action in self.top_actions]
        if duplicates := _duplicates(evidence_ids):
            raise ValueError(f"duplicate evidence IDs: {sorted(duplicates)}")
        if duplicates := _duplicates(finding_ids):
            raise ValueError(f"duplicate finding IDs: {sorted(duplicates)}")
        if duplicates := _duplicates(action_ids):
            raise ValueError(f"duplicate action IDs: {sorted(duplicates)}")

        evidence_set = set(evidence_ids)
        finding_set = set(finding_ids)
        action_set = set(action_ids)

        referenced_evidence: list[str] = []
        for finding in self.findings:
            referenced_evidence.extend(finding.evidence_ids)
            referenced_evidence.extend(finding.comparator_ids)
        for result in self.market_snapshot.results:
            referenced_evidence.append(result.evidence_id)
        for page in self.site_inventory_summary.selected_pages:
            referenced_evidence.extend(page.evidence_ids)
        for competitor in self.competitor_analysis.competitors:
            referenced_evidence.extend(competitor.evidence_ids)
        for layer in self.eight_layers:
            referenced_evidence.extend(layer.evidence_ids)
        for entry in self.version_diff.entries:
            referenced_evidence.extend(entry.evidence_ids)
        missing_evidence = set(referenced_evidence) - evidence_set
        if missing_evidence:
            raise ValueError(f"unknown evidence references: {sorted(missing_evidence)}")

        referenced_findings: list[str] = list(self.executive_decision.finding_ids)
        for action in self.top_actions:
            referenced_findings.extend(action.finding_ids)
        for layer in self.eight_layers:
            referenced_findings.extend(layer.finding_ids)
        for entry in self.version_diff.entries:
            referenced_findings.extend(entry.current_finding_ids)
        missing_findings = set(referenced_findings) - finding_set
        if missing_findings:
            raise ValueError(f"unknown finding references: {sorted(missing_findings)}")

        if [action.sequence for action in self.top_actions] != [1, 2, 3]:
            raise ValueError("top actions must be ordered with sequences 1, 2, 3")
        for action in self.top_actions:
            unknown_dependencies = set(action.dependencies) - action_set
            if unknown_dependencies:
                raise ValueError(f"unknown action dependencies: {sorted(unknown_dependencies)}")
            if action.action_id in action.dependencies:
                raise ValueError("an action cannot depend on itself")

        if self.client_summary.action_ids != action_ids:
            raise ValueError("client summary action IDs must match ordered top actions")
        roadmap_action_ids = [action_id for phase in self.roadmap_30_60_90.phases for action_id in phase.action_ids]
        if set(roadmap_action_ids) != action_set:
            raise ValueError("roadmap must reference every top action and no unknown actions")

        layer_keys = [layer.layer_key for layer in self.eight_layers]
        if tuple(layer_keys) != REQUIRED_LAYER_KEYS:
            raise ValueError("eight layers must contain the canonical ordered layer keys")

        report_type = self.report_version.report_type
        parent_report_id = self.report_version.parent_report_id
        first_party = (
            self.first_party_performance.gsc,
            self.first_party_performance.gbp,
            self.first_party_performance.ga4,
        )
        if report_type == "prospect":
            if parent_report_id is not None:
                raise ValueError("prospect reports must not have a parent report")
            if self.version_diff.kind != "initial" or self.version_diff.parent_report_id is not None:
                raise ValueError("prospect reports must use an initial version diff")
            if self.version_diff.entries:
                raise ValueError("initial prospect reports must not contain version diff entries")
            if any(source.connection_state != "not_connected" or source.snapshot_id is not None for source in first_party):
                raise ValueError("prospect reports must not contain first-party snapshots")
            if self.data_coverage.full_evidence_coverage:
                raise ValueError("prospect reports cannot claim full evidence coverage")
        else:
            if parent_report_id is None:
                raise ValueError("verified reports require a parent report")
            if self.version_diff.kind != "upgrade" or self.version_diff.parent_report_id != parent_report_id:
                raise ValueError("verified version diff must reference the report parent")
            if not self.version_diff.entries:
                raise ValueError("verified reports require version diff entries")
            for source in first_party:
                if source.connection_state == "not_connected" or source.snapshot_id is None:
                    raise ValueError("verified reports require GSC, GBP, and GA4 sync snapshots")
            for entry in self.version_diff.entries:
                if entry.previous_finding and entry.previous_finding.report_id != parent_report_id:
                    raise ValueError("previous finding references must point to the parent report")

        if self.data_coverage.full_evidence_coverage:
            if any(
                source.health_status != "healthy" or source.identity_match_status != "matched"
                for source in first_party
            ):
                raise ValueError("full evidence coverage requires healthy, matched GSC, GBP, and GA4 sources")

        if self.market_snapshot.queries != self.case_context.queries:
            raise ValueError("market snapshot queries must match case context queries")
        if self.market_snapshot.target_market != self.case_context.target_market:
            raise ValueError("market snapshot target must match case context target")

        return self
