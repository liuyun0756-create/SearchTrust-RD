"""Strict contracts for deterministic V22-074 execution planning."""

from __future__ import annotations

from datetime import date
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from app.report_v22.evidence_models import reject_nonfinite
from app.report_v22.models import (
    ActionId,
    EvidenceId,
    FindingId,
    ReportV22,
    SourceType,
    StrictModel,
)
from app.report_v22.version_diff_models import VersionDiffBuildInput, VersionDiffBuildResult
from app.report_v22.verified_reprioritization_models import (
    CandidateKind,
    QualifiedFindingRef,
    VerifiedReprioritizationInput,
    VerifiedReprioritizationResult,
)


MetricRole = Literal["primary", "guardrail"]
BaselineKind = Literal["exact_value", "band", "structural_state", "source_readiness"]
MetricEvaluator = Literal[
    "rule_no_longer_triggers",
    "sample_floor_preserved",
    "band_not_worse",
    "source_ready",
    "comparison_conflict_cleared",
]
ExecutionGate = Literal[
    "ready",
    "depends_on_previous",
    "blocked_until_measurement_ready",
    "waiting_for_action_1",
]
RoadmapPeriod = Literal["days_1_30", "days_31_60", "days_61_90"]


class ExecutionPlanLimits(StrictModel):
    max_findings: int = Field(default=25_000, ge=1, le=25_000)
    max_evidence: int = Field(default=50_000, ge=1, le=50_000)
    max_relations: int = Field(default=30_000, ge=1, le=30_000)
    max_evidence_per_metric: int = Field(default=10_000, ge=1, le=10_000)
    max_findings_per_action: int = Field(default=1_000, ge=1, le=1_000)
    max_limitations: int = Field(default=5_000, ge=1, le=5_000)
    max_audit_entries: int = Field(default=25_000, ge=3, le=25_000)
    max_bytes: int = Field(default=25_000_000, ge=1, le=25_000_000)


class ExecutionPlanBuildInput(StrictModel):
    case_id: UUID
    parent_report_id: UUID
    verified_report_id: UUID
    evaluated_at: AwareDatetime
    planning_date: date
    verified_reprioritization_input: VerifiedReprioritizationInput
    verified_reprioritization_input_checksum: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    verified_reprioritization_result: VerifiedReprioritizationResult
    verified_reprioritization_result_checksum: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    version_diff_input: VersionDiffBuildInput
    version_diff_input_checksum: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    version_diff_result: VersionDiffBuildResult
    version_diff_result_checksum: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    copy_model_version: Literal["v22_verified_copy_v1"] = "v22_verified_copy_v1"
    limits: ExecutionPlanLimits = Field(default_factory=ExecutionPlanLimits)

    @model_validator(mode="after")
    def validate_input(self) -> "ExecutionPlanBuildInput":
        if self.verified_report_id in {self.case_id, self.parent_report_id}:
            raise ValueError("verified report identity must be distinct")
        reject_nonfinite(self)
        return self


class ExecutionMetric(StrictModel):
    metric_key: str = Field(min_length=1, max_length=120, pattern=r"^[a-z][a-z0-9_]+$")
    role: MetricRole
    baseline_kind: BaselineKind
    baseline: str = Field(min_length=1, max_length=240)
    success_condition: str = Field(min_length=1, max_length=500)
    source_types: list[SourceType] = Field(min_length=1, max_length=8)
    evidence_ids: list[EvidenceId] = Field(default_factory=list, max_length=10_000)
    comparator_ids: list[EvidenceId] = Field(default_factory=list, max_length=10_000)
    finding_refs: list[QualifiedFindingRef] = Field(default_factory=list, max_length=1_000)
    minimum_sample: str | None = Field(default=None, max_length=240)
    evaluator: MetricEvaluator
    limitations: list[str] = Field(default_factory=list, max_length=40)

    @model_validator(mode="after")
    def canonical_values(self) -> "ExecutionMetric":
        self.source_types = sorted(set(self.source_types), key=(
            "site", "serp", "competitor", "gsc", "gbp", "ga4", "pagespeed", "coverage"
        ).index)
        self.evidence_ids = sorted(set(self.evidence_ids))
        self.comparator_ids = sorted(set(self.comparator_ids))
        self.finding_refs = sorted(
            self.finding_refs,
            key=lambda item: (item.origin_stage, item.ruleset_version, item.finding_id),
        )
        self.limitations = sorted(set(self.limitations))
        if set(self.evidence_ids).intersection(self.comparator_ids):
            raise ValueError("metric evidence and comparators must be disjoint")
        if self.baseline_kind in {"exact_value", "band"} and not self.evidence_ids:
            raise ValueError("value baselines require evidence")
        if self.role == "guardrail" and self.evaluator not in {"sample_floor_preserved", "band_not_worse"}:
            raise ValueError("guardrail evaluator is invalid")
        return self


class ExecutableAction(StrictModel):
    sequence: int = Field(ge=1, le=3)
    action_id: ActionId
    candidate_kind: CandidateKind
    metric_rule_id: str = Field(min_length=1, max_length=160, pattern=r"^V22\.EXECUTION\.[A-Z0-9_]+$")
    metric_rule_version: Literal["1.0.0"] = "1.0.0"
    finding_refs: list[QualifiedFindingRef] = Field(min_length=1, max_length=1_000)
    primary_metric: ExecutionMetric
    guardrail_metric: ExecutionMetric | None = None
    review_date: date
    execution_gate: ExecutionGate
    blocked_by_action_ids: list[ActionId] = Field(default_factory=list, max_length=2)

    @model_validator(mode="after")
    def validate_action(self) -> "ExecutableAction":
        self.finding_refs = sorted(
            self.finding_refs,
            key=lambda item: (item.origin_stage, item.ruleset_version, item.finding_id),
        )
        self.blocked_by_action_ids = sorted(set(self.blocked_by_action_ids))
        if self.primary_metric.role != "primary":
            raise ValueError("primary metric role mismatch")
        if self.guardrail_metric is not None and self.guardrail_metric.role != "guardrail":
            raise ValueError("guardrail metric role mismatch")
        if self.action_id in self.blocked_by_action_ids:
            raise ValueError("action cannot block itself")
        return self


class ExecutionRoadmapPhase(StrictModel):
    period: RoadmapPeriod
    action_id: ActionId
    execution_gate: ExecutionGate
    objective: str = Field(min_length=1, max_length=500)
    exit_criteria: list[str] = Field(min_length=1, max_length=40)

    @model_validator(mode="after")
    def canonical_criteria(self) -> "ExecutionRoadmapPhase":
        self.exit_criteria = list(dict.fromkeys(self.exit_criteria))
        return self


class MetricSelectionAudit(StrictModel):
    audit_id: str = Field(pattern=r"^ema_[a-z0-9][a-z0-9_-]{2,80}$")
    action_id: ActionId
    metric_key: str = Field(min_length=1, max_length=120)
    role: MetricRole
    selection_basis: Literal["verified_relation", "public_structural_fallback", "measurement_action"]
    relation_ids: list[str] = Field(default_factory=list, max_length=30_000)
    evidence_ids: list[EvidenceId] = Field(default_factory=list, max_length=10_000)
    finding_refs: list[QualifiedFindingRef] = Field(default_factory=list, max_length=1_000)
    reason_code: str = Field(pattern=r"^[A-Z][A-Z0-9_]+$", max_length=120)

    @model_validator(mode="after")
    def canonical_values(self) -> "MetricSelectionAudit":
        self.relation_ids = sorted(set(self.relation_ids))
        self.evidence_ids = sorted(set(self.evidence_ids))
        self.finding_refs = sorted(
            self.finding_refs,
            key=lambda item: (item.origin_stage, item.ruleset_version, item.finding_id),
        )
        return self


class ExecutionPlanBuildResult(StrictModel):
    schema_version: Literal["execution_plan_result_v1"] = "execution_plan_result_v1"
    ruleset_version: Literal["v22_execution_plan_v1"] = "v22_execution_plan_v1"
    metric_catalog_version: Literal["v22_execution_metrics_v1"] = "v22_execution_metrics_v1"
    copy_catalog_version: Literal["v22_verified_copy_v1"] = "v22_verified_copy_v1"
    case_id: UUID
    parent_report_id: UUID
    verified_report_id: UUID
    evaluated_at: AwareDatetime
    planning_date: date
    verified_reprioritization_input_checksum: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    verified_reprioritization_result_checksum: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    version_diff_input_checksum: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    version_diff_result_checksum: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    actions: list[ExecutableAction] = Field(min_length=3, max_length=3)
    roadmap: list[ExecutionRoadmapPhase] = Field(min_length=3, max_length=3)
    metric_audit: list[MetricSelectionAudit] = Field(min_length=3, max_length=25_000)
    report: ReportV22
    limitations: list[str] = Field(default_factory=list, max_length=5_000)

    @model_validator(mode="after")
    def validate_result(self) -> "ExecutionPlanBuildResult":
        if [item.sequence for item in self.actions] != [1, 2, 3]:
            raise ValueError("execution actions must use sequences 1, 2, 3")
        action_ids = [item.action_id for item in self.actions]
        if len(action_ids) != len(set(action_ids)):
            raise ValueError("execution action IDs must be unique")
        periods = ["days_1_30", "days_31_60", "days_61_90"]
        if [item.period for item in self.roadmap] != periods:
            raise ValueError("execution roadmap periods are not canonical")
        if [item.action_id for item in self.roadmap] != action_ids:
            raise ValueError("roadmap must preserve action order")
        if [item.action_id for item in self.report.top_actions] != action_ids:
            raise ValueError("report must preserve action order")
        if self.report.client_summary.action_ids != action_ids:
            raise ValueError("client summary must preserve action order")
        if self.report.report_version.report_id != self.verified_report_id:
            raise ValueError("report identity mismatch")
        if (
            self.report.identity.case_id != self.case_id
            or self.report.report_version.parent_report_id != self.parent_report_id
            or self.report.report_version.report_type != "verified_execution"
        ):
            raise ValueError("report binding mismatch")
        audit_keys = [(item.action_id, item.metric_key, item.role) for item in self.metric_audit]
        if len(audit_keys) != len(set(audit_keys)):
            raise ValueError("metric audits must be unique")
        known_actions = set(action_ids)
        if any(item.action_id not in known_actions for item in self.metric_audit):
            raise ValueError("metric audit references an unknown action")
        if any(item.action_id in item.blocked_by_action_ids for item in self.actions):
            raise ValueError("execution action cannot depend on itself")
        self.limitations = sorted(set(self.limitations))
        reject_nonfinite(self)
        return self
