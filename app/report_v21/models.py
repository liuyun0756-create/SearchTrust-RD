"""Pydantic models for the SearchTrust report_v2_1 contract."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


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
GBPStatusValue = Literal["checked", "not_checked", "not_found", "error"]
ComparisonResult = Literal["match", "missing", "mismatch", "partial", "not_checked"]
Confidence = Literal["high", "medium", "low"]
EvidenceSourceType = Literal[
    "page",
    "gbp",
    "schema",
    "contact_page",
    "about_page",
    "review",
    "site_internal",
    "not_available",
]

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

LAYER_LABELS: dict[str, str] = {
    "foundation": "Foundation",
    "entity_presence": "Entity Presence",
    "entity_consistency": "Entity Consistency",
    "specificity": "Specificity",
    "real_world_connection": "Real-World Connection",
    "accountability": "Accountability",
    "page_unique_value": "Page Unique Value",
    "algorithm_fit": "Algorithm Fit",
}


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GBPStatus(_StrictModel):
    status: GBPStatusValue
    gbp_url: str | None = None
    reason: str | None = None


class DataCoverage(_StrictModel):
    page_content_checked: bool
    gbp_checked: bool
    schema_checked: bool
    contact_page_checked: bool
    about_page_checked: bool
    reviews_checked: bool
    internal_pages_checked: bool
    competitor_pages_checked: bool
    limitations: list[str] = Field(default_factory=list)


class OverallStatus(_StrictModel):
    label: str
    level: Literal["weak", "medium_weak", "medium", "strong", "high"]
    explanation: str


class RankingPotential(_StrictModel):
    label: str
    level: Literal["low", "competitive", "improvable", "strong"]
    explanation: str


class RiskLevel(_StrictModel):
    label: str
    level: Literal["low", "medium", "medium_high", "high"]
    explanation: str


class EvidenceItem(_StrictModel):
    id: str
    source_type: EvidenceSourceType
    source_label: str
    source_url: str | None = None
    page_section: str | None = None
    extracted_text: str | None = None
    normalized_value: str | None = None
    expected_value: str | None = None
    comparison_result: ComparisonResult
    confidence: Confidence
    explanation: str


class ActionItem(_StrictModel):
    id: str
    priority: Literal["high", "medium", "low"]
    task_title: str
    affected_layer: LayerKey
    related_rule_ids: list[int] = Field(default_factory=list)
    where_to_add: list[str] = Field(default_factory=list)
    what_to_add: list[str] = Field(default_factory=list)
    example_copy: str
    implementation_notes: list[str] = Field(default_factory=list)
    completion_signals: list[str] = Field(default_factory=list)
    expected_effect: str
    effort_level: Literal["small", "medium", "large"]


class PrimaryBlockingLayer(_StrictModel):
    layer_key: LayerKey
    layer_name: str
    reason: str
    evidence_items: list[EvidenceItem] = Field(default_factory=list)


class PageLevel(_StrictModel):
    label: str
    what_it_looks_like: str
    strengths: list[str] = Field(default_factory=list)
    missing_elements: list[str] = Field(default_factory=list)


class LayerFinding(_StrictModel):
    layer_id: int = Field(ge=1, le=8)
    layer_key: LayerKey
    layer_name: str
    layer_label: str
    status: LayerStatus
    checked_rule_ids: list[int] = Field(default_factory=list)
    triggered_rule_ids: list[int] = Field(default_factory=list)
    summary: str
    explanation: str
    evidence_items: list[EvidenceItem] = Field(default_factory=list)
    suggested_fixes: list[str] = Field(default_factory=list)
    action_items: list[ActionItem] = Field(default_factory=list)


class KeyIssue(_StrictModel):
    id: str
    issue_title: str
    affected_layer: LayerKey
    related_rule_ids: list[int] = Field(default_factory=list)
    severity: Literal["high", "medium", "low"]
    evidence_items: list[EvidenceItem] = Field(default_factory=list)
    explanation: str
    why_it_matters: str
    recommended_actions: list[ActionItem] = Field(default_factory=list)


class RoadmapPhase(_StrictModel):
    id: str
    phase_title: str
    sequence: int = Field(ge=1)
    goal: str
    entry_condition: str
    action_items: list[ActionItem] = Field(default_factory=list)
    expected_outcomes: list[str] = Field(default_factory=list)


class OptimizationPath(_StrictModel):
    must_execute_now: list[ActionItem] = Field(default_factory=list)
    defer_until_later: list[ActionItem] = Field(default_factory=list)
    do_not_prioritize_yet: list[ActionItem] = Field(default_factory=list)
    roadmap: list[RoadmapPhase] = Field(default_factory=list)
    fix_order_warning: str
    completion_signals: list[str] = Field(default_factory=list)


class ClientSummary(_StrictModel):
    title: str
    plain_language_summary: str
    why_it_matters: str
    first_priority: str
    not_first_priority: str
    expected_change: str


class ReportV21(_StrictModel):
    schema_version: Literal["2.1"]
    report_id: str = Field(min_length=1)
    analyzed_url: str = Field(min_length=1)
    page_type: str = Field(min_length=1)
    generated_at: str = Field(min_length=1)
    gbp_status: GBPStatus
    data_coverage: DataCoverage
    overall_status: OverallStatus
    ranking_potential: RankingPotential
    risk_level: RiskLevel
    primary_blocking_layer: PrimaryBlockingLayer
    page_level: PageLevel
    layers: list[LayerFinding] = Field(min_length=8, max_length=8)
    key_issues: list[KeyIssue] = Field(default_factory=list)
    optimization_path: OptimizationPath
    client_summary: ClientSummary
