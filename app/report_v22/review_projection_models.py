"""Typed reading layers: private statements never become client copy by accident."""
from app.report_v22.review_plan_basis import PlanAction
from typing import Literal
from uuid import UUID

from pydantic import Field

from app.report_v22.capability import Checksum, EditableAction, Label, Timestamp
from app.report_v22.comparison_base import StrictModel
from app.report_v22.comparison_models import ComparisonScope, MetricComparison
from app.report_v22.comparison_windows import ComparisonWindows
from app.report_v22.ga4_comparison_models import Ga4ComparisonScope
from app.report_v22.ga4_session_reading import MAX_COUNT, Reason
from app.report_v22.implementation_records import ImplementationEntry
from app.report_v22.metric_reading import MetricReading
from app.report_v22.page_implementation_models import PageCondition, PageConditionResult, ConditionChange, Failure
from app.report_v22.review_evidence_models import ReviewDecision, Disposition


class RecordedFacts(StrictModel):
    state: Literal["seo_recorded", "not_recorded"]
    implemented_on: str | None
    timezone: str | None
    system_completion_verified: Literal[False] = False


class PageFacts(StrictModel):
    target_url: str
    observed_at: Timestamp
    previous_observed_at: Timestamp | None
    conditions: list[PageCondition]
    results: list[PageConditionResult]
    status: Literal["checked", "unable_to_check"]
    failure: Failure | None
    comparison: Literal["first_observation", "not_comparable", "compared"]
    comparison_reason: Literal["no_baseline", "invalid_baseline", "different_plan", "invalid_time_order", "check_unavailable"] | None
    changes: list[ConditionChange]
    limitation: Literal["static_response_only_rendering_functionality_and_identity_not_verified"]


class GscFacts(StrictModel):
    scope: ComparisonScope
    windows: ComparisonWindows
    evaluated_at: Timestamp
    state: Literal["waiting", "insufficient", "not_comparable", "comparable"]
    reasons: list[Label]
    metrics: list[MetricComparison]
    reading: list[MetricReading]
    limitations: list[str]
    concurrent_changes: Literal["unknown", "reported", "none_reported"]
    effect_eligibility: Literal["pending_metric_specific_calibration"]


class Ga4Facts(StrictModel):
    scope: Ga4ComparisonScope
    windows: ComparisonWindows
    started_at: Timestamp
    completed_at: Timestamp
    state: Literal["available", "withheld"]
    reasons: list[Reason] = Field(max_length=10)
    before: int | None = Field(ge=0, le=MAX_COUNT)
    after: int | None = Field(ge=0, le=MAX_COUNT)
    difference: int | None = Field(ge=-MAX_COUNT, le=MAX_COUNT)
    relative_change_percent: str | None = Field(pattern=r"^-?(0|[1-9][0-9]*)\.[0-9]{2}$", max_length=24)
    processing_completeness: Literal["not_attested"]
    measurement_history: Literal["unverified"]


CheckCode = Literal["CONFIRM_RECORDED_WORK", "ESTABLISH_PAGE_CHECK", "FIRST_PAGE_BASELINE",
    "REVIEW_PAGE_CONDITIONS", "WAIT_FOR_WINDOW", "REVIEW_GSC_LIMITS", "REVIEW_GA4_LIMITS",
    "NO_SELECTED_METRICS", "DESCRIPTIVE_COUNTS_ONLY"]


class ReviewCheck(StrictModel):
    code: CheckCode
    text: str


class ActionFacts(StrictModel):
    action_id: Label
    title: Label
    target: str
    recorded_work: RecordedFacts
    page: PageFacts | None
    gsc: GscFacts | None
    ga4: Ga4Facts | None
    checks: list[ReviewCheck]
    effect: None = None
    causal_attribution: Literal[False] = False


class DecisionSummary(StrictModel):
    action_id: Label
    decision_state: Literal["needs_review", "seo_recorded"]
    disposition: Disposition | None
    text: str
    system_completion_verified: Literal[False] = False


class ClientAction(StrictModel):
    facts: ActionFacts
    next_decision: DecisionSummary


class SeoAction(StrictModel):
    facts: ActionFacts
    approved_action: PlanAction
    implementation_entry: ImplementationEntry | None
    source_references: list[Label]
    next_decision: DecisionSummary
    decision_record: ReviewDecision | None


class PriorFinding(StrictModel):
    classification: Literal["fact", "inference", "estimate"]
    target: str
    observed_fact: str
    observed_at: Timestamp
    comparison_scope: str
    judgment: str
    interpretation: Literal["supported", "needs_verification", "conflicting"]
    limitations: list[str]


class PriorDiagnosis(StrictModel):
    source: Literal["prior_report_not_rechecked"]
    title: str
    summary: str
    action_state: Literal["actions_supported", "insufficient_basis"]
    findings: list[PriorFinding]
    limitations: list[str]


class ReviewProjection(StrictModel):
    schema_version: Literal["v22_review_projection_v1"]
    rules_version: Literal["v22_review_projection_rules_v1"]
    input_checksum: Checksum
    evidence_checksum: Checksum
    evaluated_at: Timestamp
    user_id: UUID
    case_id: UUID
    report_id: UUID
    report_checksum: Checksum
    delivery_id: UUID
    delivery_revision: int
    delivery_checksum: Checksum
    prior_diagnosis: PriorDiagnosis
    seo_actions: list[SeoAction] = Field(max_length=3)
    seo_new_decisions: list[ReviewDecision] = Field(max_length=3)
    client_actions: list[ClientAction] = Field(max_length=3)
    client_new_decisions: list[DecisionSummary] = Field(max_length=3)
    effect: None = None
    causal_attribution: Literal[False] = False
    checksum: Checksum


class ActionDifference(StrictModel):
    action_id: Label
    evidence_changed: bool
    decision_changed: bool
    changed_sections: list[Literal["approved_work", "recorded_work", "page", "gsc", "ga4", "checks", "decision"]]


class ReviewDifference(StrictModel):
    schema_version: Literal["v22_review_difference_v1"]
    state: Literal["first_baseline", "not_comparable", "compared"]
    reason: Literal["no_previous_projection", "different_review_basis"] | None
    previous_input_checksum: Checksum | None
    current_input_checksum: Checksum
    actions: list[ActionDifference] = Field(max_length=3)
    new_decisions_changed: bool
