"""Strict contracts for deterministic V22-072 verified reprioritization."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from app.report_v22.action_models import ActionPriority, ActionSkeleton, PublicActionLimits, PublicActionPlan
from app.report_v22.cross_source_findings_models import CrossSourceFindingsLimits, CrossSourceFindingsResult
from app.report_v22.evidence_models import reject_nonfinite
from app.report_v22.findings_models import PublicFindingsInput, PublicFindingsResult
from app.report_v22.first_party_findings_models import FirstPartyFindingsInput, FirstPartyFindingsResult
from app.report_v22.models import ActionId, FindingId, ImplementationStep, StrictModel


FindingOrigin = Literal["public_findings", "first_party_findings", "cross_source_findings"]
RelationKind = Literal["supports", "reduces_urgency", "measurement_conflict", "unmatched"]
CandidateKind = Literal["existing_public", "measurement_repair", "measurement_consistency"]
VerificationLevel = Literal[
    "cross_source_supported", "single_source_supported", "measurement_conflict",
    "public_only", "reduced_by_growth", "forced_measurement_repair",
]


class VerifiedReprioritizationLimits(StrictModel):
    max_public_candidates: int = Field(default=1_000, ge=3, le=1_000)
    max_new_findings: int = Field(default=15_000, ge=1, le=15_000)
    max_relations: int = Field(default=30_000, ge=1, le=30_000)
    max_audit_entries: int = Field(default=2_000, ge=3, le=2_000)
    max_issue_codes: int = Field(default=100, ge=1, le=100)
    max_bytes: int = Field(default=20_000_000, ge=1, le=20_000_000)


class VerifiedReprioritizationInput(StrictModel):
    case_id: UUID
    parent_report_id: UUID
    evaluated_at: AwareDatetime
    planning_date: date
    public_findings_input: PublicFindingsInput
    public_findings_result: PublicFindingsResult
    public_findings_input_checksum: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    public_findings_result_checksum: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    public_action_plan: PublicActionPlan
    public_action_limits: PublicActionLimits = Field(default_factory=PublicActionLimits)
    public_action_plan_checksum: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    first_party_input: FirstPartyFindingsInput
    first_party_result: FirstPartyFindingsResult
    first_party_input_checksum: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    first_party_result_checksum: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    cross_source_normalized_domain: str = Field(
        min_length=1, max_length=253, pattern=r"^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$"
    )
    cross_source_limits: CrossSourceFindingsLimits = Field(default_factory=CrossSourceFindingsLimits)
    cross_source_result: CrossSourceFindingsResult
    cross_source_result_checksum: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    limits: VerifiedReprioritizationLimits = Field(default_factory=VerifiedReprioritizationLimits)

    @model_validator(mode="after")
    def finite(self) -> "VerifiedReprioritizationInput":
        reject_nonfinite(self)
        return self


class QualifiedFindingRef(StrictModel):
    origin_stage: FindingOrigin
    ruleset_version: str = Field(min_length=1, max_length=64)
    finding_id: FindingId


class FindingRelation(StrictModel):
    relation_id: str = Field(pattern=r"^rel_[a-z0-9][a-z0-9_-]{2,80}$")
    finding_ref: QualifiedFindingRef
    relation_kind: RelationKind
    candidate_key: str | None = Field(default=None, max_length=200)
    action_id: ActionId | None = None
    target_key: str = Field(min_length=1, max_length=500)
    reason_code: str = Field(min_length=1, max_length=120, pattern=r"^[A-Z][A-Z0-9_]+$")

    @model_validator(mode="after")
    def validate_candidate(self) -> "FindingRelation":
        matched = self.relation_kind != "unmatched"
        if matched != (self.candidate_key is not None and self.action_id is not None):
            raise ValueError("matched relations require a candidate and action")
        return self


class MeasurementAction(StrictModel):
    action_id: ActionId
    template_key: Literal["restore_verified_measurement", "review_measurement_consistency"]
    template_version: Literal["1.0.0"] = "1.0.0"
    source_types: list[Literal["gsc", "ga4", "gbp"]] = Field(min_length=1, max_length=3)
    issue_codes: list[str] = Field(min_length=1, max_length=100)
    finding_refs: list[QualifiedFindingRef] = Field(default_factory=list, max_length=25)
    implementation_steps: list[ImplementationStep] = Field(min_length=1, max_length=10)
    definition_of_done: list[str] = Field(min_length=1, max_length=10)
    review_date: date

    @model_validator(mode="after")
    def canonical_values(self) -> "MeasurementAction":
        order = {"gsc": 0, "ga4": 1, "gbp": 2}
        self.source_types = sorted(set(self.source_types), key=order.__getitem__)
        self.issue_codes = sorted(set(self.issue_codes))
        self.finding_refs = sorted(
            self.finding_refs,
            key=lambda item: (item.origin_stage, item.ruleset_version, item.finding_id),
        )
        if [step.sequence for step in self.implementation_steps] != list(range(1, len(self.implementation_steps) + 1)):
            raise ValueError("measurement action steps must be contiguous")
        return self


class RankedVerifiedAction(StrictModel):
    sequence: int = Field(ge=1, le=3)
    action_id: ActionId
    candidate_kind: CandidateKind
    verification_level: VerificationLevel
    verification_rank: int = Field(ge=0, le=100)
    relation_ids: list[str] = Field(default_factory=list, max_length=30_000)
    public_action: ActionSkeleton | None = None
    measurement_action: MeasurementAction | None = None

    @model_validator(mode="after")
    def validate_payload(self) -> "RankedVerifiedAction":
        self.relation_ids = sorted(set(self.relation_ids))
        public = self.public_action is not None
        if public == (self.measurement_action is not None):
            raise ValueError("ranked action must contain exactly one action payload")
        if public != (self.candidate_kind == "existing_public"):
            raise ValueError("ranked action kind does not match payload")
        payload = self.public_action or self.measurement_action
        if payload is None or payload.action_id != self.action_id:
            raise ValueError("ranked action identity mismatch")
        if self.public_action is not None and self.public_action.sequence != self.sequence:
            raise ValueError("public action sequence mismatch")
        return self


class VerifiedSelectionAudit(StrictModel):
    candidate_key: str = Field(min_length=1, max_length=200)
    action_id: ActionId
    candidate_kind: CandidateKind
    public_anchor: QualifiedFindingRef | None = None
    base_priority: ActionPriority | None = None
    verification_level: VerificationLevel
    verification_rank: int = Field(ge=0, le=100)
    relation_ids: list[str] = Field(default_factory=list, max_length=30_000)
    selection_state: Literal["selected", "unselected"]
    reason: Literal[
        "forced_measurement_repair", "selected_verified_top_three", "selected_business_fallback",
        "lower_verified_priority", "excluded_when_measurement_blocked",
    ]

    @model_validator(mode="after")
    def validate_shape(self) -> "VerifiedSelectionAudit":
        self.relation_ids = sorted(set(self.relation_ids))
        if self.candidate_kind == "existing_public" and (
            self.public_anchor is None or self.base_priority is None
        ):
            raise ValueError("public audit requires its anchor and base priority")
        if self.candidate_kind != "existing_public" and (
            self.public_anchor is not None or self.base_priority is not None
        ):
            raise ValueError("measurement audit cannot claim public priority")
        selected_reason = self.reason.startswith("selected_") or self.reason == "forced_measurement_repair"
        if (self.selection_state == "selected") != selected_reason:
            raise ValueError("selection reason and state do not match")
        return self


class UnusedFinding(StrictModel):
    finding_ref: QualifiedFindingRef
    reason: Literal["no_strict_target_match", "audit_only_rule", "not_selected"]


class VerifiedReprioritizationResult(StrictModel):
    schema_version: Literal["verified_reprioritization_v1"] = "verified_reprioritization_v1"
    ruleset_version: Literal["v22_verified_reprioritization_v1"] = "v22_verified_reprioritization_v1"
    public_action_catalog_version: Literal["v22_public_actions_v1"] = "v22_public_actions_v1"
    case_id: UUID
    parent_report_id: UUID
    evaluated_at: AwareDatetime
    planning_date: date
    public_findings_result_checksum: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    public_action_plan_checksum: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    first_party_result_checksum: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    cross_source_result_checksum: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    actions: list[RankedVerifiedAction] = Field(min_length=3, max_length=3)
    core_problem_finding: QualifiedFindingRef
    relations: list[FindingRelation] = Field(max_length=30_000)
    selection_audit: list[VerifiedSelectionAudit] = Field(min_length=3, max_length=2_000)
    unused_findings: list[UnusedFinding] = Field(default_factory=list, max_length=15_000)

    @model_validator(mode="after")
    def validate_result(self) -> "VerifiedReprioritizationResult":
        if [item.sequence for item in self.actions] != [1, 2, 3]:
            raise ValueError("verified actions must use ordered sequences 1, 2, 3")
        action_ids = [item.action_id for item in self.actions]
        if len(action_ids) != len(set(action_ids)):
            raise ValueError("verified action IDs must be unique")
        relation_ids = [item.relation_id for item in self.relations]
        if len(relation_ids) != len(set(relation_ids)):
            raise ValueError("relation IDs must be unique")
        known_relations = set(relation_ids)
        if any(not set(item.relation_ids) <= known_relations for item in self.actions + self.selection_audit):
            raise ValueError("action or audit references an unknown relation")
        audit_ids = [item.action_id for item in self.selection_audit]
        if len(audit_ids) != len(set(audit_ids)):
            raise ValueError("selection audit action IDs must be unique")
        selected = {item.action_id for item in self.selection_audit if item.selection_state == "selected"}
        if selected != set(action_ids):
            raise ValueError("selection audit must identify the selected actions")
        audits = {item.action_id: item for item in self.selection_audit}
        relations = {item.relation_id: item for item in self.relations}
        candidate_keys = {item.candidate_key for item in self.selection_audit}
        if len(candidate_keys) != len(self.selection_audit):
            raise ValueError("selection audit candidate keys must be unique")
        if any(
            item.relation_kind != "unmatched"
            and (
                item.action_id not in audits
                or item.candidate_key != audits[item.action_id].candidate_key
            )
            for item in self.relations
        ):
            raise ValueError("matched relation references an unknown candidate")
        for audit in self.selection_audit:
            if any(
                relations[key].action_id != audit.action_id
                or relations[key].candidate_key != audit.candidate_key
                for key in audit.relation_ids
            ):
                raise ValueError("audit relation does not belong to its candidate")
        for action in self.actions:
            audit = audits[action.action_id]
            if (
                audit.candidate_kind != action.candidate_kind
                or audit.verification_level != action.verification_level
                or audit.verification_rank != action.verification_rank
                or audit.relation_ids != action.relation_ids
                or any(relations[key].action_id != action.action_id for key in action.relation_ids)
            ):
                raise ValueError("selected action and audit do not match")
            if action.measurement_action is not None:
                relation_refs = [relations[key].finding_ref for key in action.relation_ids]
                if not set(map(str, action.measurement_action.finding_refs)) <= set(map(str, relation_refs)):
                    raise ValueError("measurement action references an unbound Finding")
        if self.core_problem_finding.origin_stage != "public_findings":
            raise ValueError("core problem must remain a public Finding")
        public_selected = [item for item in self.selection_audit if item.action_id in selected and item.public_anchor]
        if self.core_problem_finding not in [item.public_anchor for item in public_selected]:
            raise ValueError("core problem must anchor a selected business action")
        core_audit = next(item for item in public_selected if item.public_anchor == self.core_problem_finding)
        if core_audit.verification_rank != max(item.verification_rank for item in public_selected):
            raise ValueError("core problem must use the strongest selected business action")
        unused_keys = [
            (item.finding_ref.origin_stage, item.finding_ref.ruleset_version, item.finding_ref.finding_id)
            for item in self.unused_findings
        ]
        if len(unused_keys) != len(set(unused_keys)):
            raise ValueError("unused Finding references must be unique")
        try:
            expected_dates = [self.planning_date + timedelta(days=30 * index) for index in (1, 2, 3)]
        except OverflowError as exc:
            raise ValueError("verified review date overflow") from exc
        dates = [
            (item.public_action.review_date if item.public_action else item.measurement_action.review_date)
            for item in self.actions
        ]
        if dates != expected_dates:
            raise ValueError("verified review dates must be planning date plus 30, 60 and 90 days")
        reject_nonfinite(self)
        return self
