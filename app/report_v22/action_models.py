"""Strict internal contracts for deterministic v2.2 public action planning."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Literal

from pydantic import Field, HttpUrl, model_validator

from app.collectors.site_inventory_models import SitePageType
from app.report_v22.findings_models import PublicFindingsResult
from app.report_v22.models import (
    ActionId,
    ActionSpecification,
    EffortBucket,
    FindingId,
    ImplementationStep,
    SourceType,
    StrictModel,
)


GbpActionField = Literal["business_name", "address", "phone", "service_area"]
ActionTargetKind = Literal["url", "query", "page_type", "gbp_field", "site"]
AllowedFactField = Literal[
    "finding.statement",
    "finding.scope",
    "finding.severity",
    "finding.confidence",
    "target.url",
    "target.query",
    "target.page_type",
    "target.gbp_field",
    "target.site",
    "action.definition_of_done",
]


class PublicActionLimits(StrictModel):
    max_findings: int = Field(default=10_000, ge=1, le=10_000)
    max_candidates: int = Field(default=1_000, ge=3, le=1_000)
    max_findings_per_action: int = Field(default=1_000, ge=1, le=1_000)
    max_targets_per_action: int = Field(default=1_000, ge=1, le=1_000)
    max_steps_per_action: int = Field(default=20, ge=1, le=20)
    max_requirements_per_group: int = Field(default=50, ge=1, le=50)
    max_assets_per_action: int = Field(default=50, ge=0, le=50)
    max_metrics_per_action: int = Field(default=20, ge=1, le=20)
    max_audit_entries: int = Field(default=1_000, ge=3, le=1_000)
    max_bytes: int = Field(default=20_000_000, ge=1, le=20_000_000)


class PublicActionPlanInput(StrictModel):
    findings_result: PublicFindingsResult
    planning_date: date
    limits: PublicActionLimits = Field(default_factory=PublicActionLimits)


class ActionTarget(StrictModel):
    kind: ActionTargetKind
    url: HttpUrl | None = None
    query: str | None = Field(default=None, min_length=1, max_length=300)
    page_type: SitePageType | None = None
    gbp_field: GbpActionField | None = None
    finding_ids: list[FindingId] = Field(min_length=1, max_length=1_000)

    @model_validator(mode="after")
    def validate_target(self) -> "ActionTarget":
        self.finding_ids = sorted(set(self.finding_ids))
        values = {
            "url": self.url,
            "query": self.query,
            "page_type": self.page_type,
            "gbp_field": self.gbp_field,
        }
        expected = "url" if self.kind == "site" else self.kind
        if any(value is not None for key, value in values.items() if key != expected):
            raise ValueError("action target contains a value for another target kind")
        if values[expected] is None:
            raise ValueError("action target is missing its required value")
        return self


class ActionValidationMetric(StrictModel):
    metric_key: str = Field(min_length=1, max_length=120)
    baseline: str = Field(min_length=1, max_length=240)
    success_condition: str = Field(min_length=1)
    source_types: list[SourceType] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def canonical_sources(self) -> "ActionValidationMetric":
        if len(self.source_types) != len(set(self.source_types)):
            raise ValueError("validation metric source types must be unique")
        return self


class ActionCopyRequirements(StrictModel):
    finding_ids: list[FindingId] = Field(min_length=1, max_length=1_000)
    allowed_fact_fields: list[AllowedFactField] = Field(min_length=1, max_length=20)
    required_limitations: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def canonical_values(self) -> "ActionCopyRequirements":
        self.finding_ids = sorted(set(self.finding_ids))
        if len(self.allowed_fact_fields) != len(set(self.allowed_fact_fields)):
            raise ValueError("copy fact fields must be unique")
        if len(self.required_limitations) != len(set(self.required_limitations)):
            raise ValueError("copy limitations must be unique")
        return self


class ActionSkeleton(StrictModel):
    action_id: ActionId
    sequence: int = Field(ge=1, le=3)
    finding_ids: list[FindingId] = Field(min_length=1, max_length=1_000)
    template_key: str = Field(min_length=1, max_length=80, pattern=r"^[a-z][a-z0-9_]+$")
    template_version: str = Field(min_length=1, max_length=32)
    exact_targets: list[ActionTarget] = Field(min_length=1, max_length=1_000)
    implementation_steps: list[ImplementationStep] = Field(min_length=1, max_length=20)
    specification: ActionSpecification
    required_client_assets: list[str] = Field(default_factory=list, max_length=50)
    dependencies: list[ActionId] = Field(default_factory=list, max_length=2)
    owner_suggestion: str = Field(min_length=1, max_length=160)
    effort_bucket: EffortBucket
    definition_of_done: list[str] = Field(min_length=1, max_length=20)
    validation_metrics: list[ActionValidationMetric] = Field(min_length=1, max_length=20)
    data_sources: list[SourceType] = Field(min_length=1, max_length=8)
    review_date: date
    copy_requirements: ActionCopyRequirements

    @model_validator(mode="after")
    def validate_action(self) -> "ActionSkeleton":
        self.finding_ids = sorted(set(self.finding_ids))
        if [step.sequence for step in self.implementation_steps] != list(
            range(1, len(self.implementation_steps) + 1)
        ):
            raise ValueError("implementation step sequences must be contiguous and ordered")
        if len(self.dependencies) != len(set(self.dependencies)):
            raise ValueError("action dependencies must be unique")
        if len(self.data_sources) != len(set(self.data_sources)):
            raise ValueError("action data sources must be unique")
        if self.copy_requirements.finding_ids != self.finding_ids:
            raise ValueError("copy requirements must reference the action findings")
        if any(not set(target.finding_ids) <= set(self.finding_ids) for target in self.exact_targets):
            raise ValueError("action target references an unknown finding")
        target_keys = [
            (target.kind, str(target.url), target.query, target.page_type, target.gbp_field)
            for target in self.exact_targets
        ]
        if len(target_keys) != len(set(target_keys)):
            raise ValueError("action targets must be unique")
        return self


class ActionPriority(StrictModel):
    severity_rank: int = Field(ge=1, le=4)
    confidence_rank: int = Field(ge=1, le=3)
    classification_rank: int = Field(ge=1, le=3)
    impact_scope: int = Field(ge=0, le=15)


class ActionSelectionAudit(StrictModel):
    candidate_key: str = Field(min_length=1, max_length=200)
    action_id: ActionId
    template_key: str = Field(min_length=1, max_length=80)
    anchor_finding_id: FindingId
    finding_ids: list[FindingId] = Field(min_length=1, max_length=1_000)
    priority: ActionPriority
    selection_state: Literal["selected", "unselected"]
    reason: Literal["selected_top_three", "lower_priority"]

    @model_validator(mode="after")
    def validate_selection(self) -> "ActionSelectionAudit":
        self.finding_ids = sorted(set(self.finding_ids))
        if self.anchor_finding_id not in self.finding_ids:
            raise ValueError("selection anchor must be one of the candidate findings")
        if (self.selection_state == "selected") != (self.reason == "selected_top_three"):
            raise ValueError("selection state and reason do not match")
        return self


class PublicActionPlan(StrictModel):
    schema_version: Literal["public_action_plan_v1"] = "public_action_plan_v1"
    action_catalog_version: Literal["v22_public_actions_v1"] = "v22_public_actions_v1"
    source_ruleset_version: Literal["v22_public_findings_v1"] = "v22_public_findings_v1"
    planning_date: date
    findings_checksum: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    actions: list[ActionSkeleton] = Field(min_length=3, max_length=3)
    selection_audit: list[ActionSelectionAudit] = Field(min_length=3, max_length=1_000)
    unselected_finding_ids: list[FindingId] = Field(default_factory=list, max_length=10_000)

    @model_validator(mode="after")
    def validate_plan(self) -> "PublicActionPlan":
        action_ids = [action.action_id for action in self.actions]
        if [action.sequence for action in self.actions] != [1, 2, 3]:
            raise ValueError("public actions must use ordered sequences 1, 2, 3")
        if len(action_ids) != len(set(action_ids)):
            raise ValueError("public action IDs must be unique")
        action_set = set(action_ids)
        sequence_by_id = {action.action_id: action.sequence for action in self.actions}
        for action in self.actions:
            if action.action_id in action.dependencies:
                raise ValueError("an action cannot depend on itself")
            if not set(action.dependencies) <= action_set:
                raise ValueError("action dependencies must reference selected actions")
            if any(sequence_by_id[key] >= action.sequence for key in action.dependencies):
                raise ValueError("action dependencies must appear earlier")
            try:
                expected_date = self.planning_date + timedelta(days=30 * action.sequence)
            except OverflowError as exc:
                raise ValueError("action review date overflow") from exc
            if action.review_date != expected_date:
                raise ValueError("action review dates must be planning date plus 30, 60 and 90 days")
        selected_audit = [item.action_id for item in self.selection_audit if item.selection_state == "selected"]
        if len(self.selection_audit) != len({item.candidate_key for item in self.selection_audit}):
            raise ValueError("selection audit candidate keys must be unique")
        if len(self.selection_audit) != len({item.action_id for item in self.selection_audit}):
            raise ValueError("selection audit action IDs must be unique")
        if set(selected_audit) != action_set or len(selected_audit) != 3:
            raise ValueError("selection audit must identify exactly the selected actions")
        audit_by_action = {item.action_id: item for item in self.selection_audit}
        for action in self.actions:
            audit = audit_by_action[action.action_id]
            if audit.template_key != action.template_key or audit.finding_ids != action.finding_ids:
                raise ValueError("selected action and selection audit do not match")
        selected_findings = {key for action in self.actions for key in action.finding_ids}
        if len(self.unselected_finding_ids) != len(set(self.unselected_finding_ids)):
            raise ValueError("unselected finding IDs must be unique")
        if selected_findings.intersection(self.unselected_finding_ids):
            raise ValueError("selected and unselected findings must be disjoint")
        audited_unselected = sorted(
            {
                finding_id
                for item in self.selection_audit
                if item.selection_state == "unselected"
                for finding_id in item.finding_ids
            }
        )
        if self.unselected_finding_ids != audited_unselected:
            raise ValueError("unselected findings must match the selection audit")
        return self
