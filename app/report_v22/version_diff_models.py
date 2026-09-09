"""Strict contracts for deterministic V22-073 version differences."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from app.report_v22.evidence_models import reject_nonfinite
from app.report_v22.models import (
    ActionId,
    EvidenceId,
    FindingId,
    PreviousFindingReference,
    ReportV22,
    StrictModel,
    VersionChangeType,
    VersionDiff,
)
from app.report_v22.verified_reprioritization_models import (
    QualifiedFindingRef,
    VerifiedReprioritizationInput,
    VerifiedReprioritizationResult,
)


class VersionDiffLimits(StrictModel):
    max_parent_findings: int = Field(default=10_000, ge=1, le=10_000)
    max_new_findings: int = Field(default=15_000, ge=1, le=15_000)
    max_current_evidence: int = Field(default=50_000, ge=1, le=50_000)
    max_relations: int = Field(default=30_000, ge=1, le=30_000)
    max_entries: int = Field(default=25_000, ge=1, le=25_000)
    max_audit_entries: int = Field(default=25_000, ge=1, le=25_000)
    max_current_findings_per_entry: int = Field(default=1_000, ge=1, le=1_000)
    max_evidence_per_entry: int = Field(default=10_000, ge=1, le=10_000)
    max_bytes: int = Field(default=20_000_000, ge=1, le=20_000_000)


class VersionDiffBuildInput(StrictModel):
    case_id: UUID
    parent_report_id: UUID
    evaluated_at: AwareDatetime
    parent_report: ReportV22
    parent_report_checksum: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    verified_reprioritization_input: VerifiedReprioritizationInput
    verified_reprioritization_input_checksum: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    verified_reprioritization_result: VerifiedReprioritizationResult
    verified_reprioritization_result_checksum: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    limits: VersionDiffLimits = Field(default_factory=VersionDiffLimits)

    @model_validator(mode="after")
    def finite(self) -> "VersionDiffBuildInput":
        reject_nonfinite(self)
        return self


EvidenceBasis = Literal["direct_relation", "ranking_boundary", "parent_fallback", "new_finding"]


class VersionDiffAuditEntry(StrictModel):
    audit_id: str = Field(pattern=r"^vda_[a-z0-9][a-z0-9_-]{2,80}$")
    change_type: VersionChangeType
    previous_finding_id: FindingId | None = None
    current_finding_refs: list[QualifiedFindingRef] = Field(min_length=1, max_length=1_000)
    relation_ids: list[str] = Field(default_factory=list, max_length=30_000)
    action_id: ActionId | None = None
    reason_code: str = Field(pattern=r"^[A-Z][A-Z0-9_]+$", max_length=120)
    decision_issue_codes: list[str] = Field(default_factory=list, max_length=100)
    evidence_ids: list[EvidenceId] = Field(min_length=1, max_length=10_000)
    evidence_basis: EvidenceBasis

    @model_validator(mode="after")
    def canonical_values(self) -> "VersionDiffAuditEntry":
        self.current_finding_refs = sorted(
            self.current_finding_refs,
            key=lambda item: (item.origin_stage, item.ruleset_version, item.finding_id),
        )
        self.relation_ids = sorted(set(self.relation_ids))
        self.decision_issue_codes = sorted(set(self.decision_issue_codes))
        self.evidence_ids = sorted(set(self.evidence_ids))
        if (self.change_type == "new") != (self.previous_finding_id is None):
            raise ValueError("only new changes omit the previous Finding")
        return self


class VersionDiffBuildResult(StrictModel):
    schema_version: Literal["version_diff_result_v1"] = "version_diff_result_v1"
    ruleset_version: Literal["v22_version_diff_v1"] = "v22_version_diff_v1"
    reason_catalog_version: Literal["v22_version_diff_reasons_v1"] = "v22_version_diff_reasons_v1"
    case_id: UUID
    parent_report_id: UUID
    evaluated_at: AwareDatetime
    parent_report_checksum: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    verified_reprioritization_input_checksum: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    verified_reprioritization_result_checksum: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    version_diff: VersionDiff
    audit: list[VersionDiffAuditEntry] = Field(max_length=25_000)
    unchanged_previous_findings: list[PreviousFindingReference] = Field(max_length=10_000)
    consumed_new_findings: list[QualifiedFindingRef] = Field(max_length=15_000)
    new_findings: list[QualifiedFindingRef] = Field(max_length=15_000)

    @model_validator(mode="after")
    def validate_result(self) -> "VersionDiffBuildResult":
        if self.version_diff.kind != "upgrade" or self.version_diff.parent_report_id != self.parent_report_id:
            raise ValueError("version difference must be bound to its parent")
        if len(self.version_diff.entries) != len(self.audit):
            raise ValueError("every visible difference requires one audit entry")
        for entry, audit in zip(self.version_diff.entries, self.audit, strict=True):
            previous_id = entry.previous_finding.finding_id if entry.previous_finding else None
            if (
                entry.change_type != audit.change_type
                or previous_id != audit.previous_finding_id
                or entry.evidence_ids != audit.evidence_ids
                or set(entry.current_finding_ids)
                != {item.finding_id for item in audit.current_finding_refs}
            ):
                raise ValueError("visible difference and audit entry must match")
            if entry.previous_finding and entry.previous_finding.report_id != self.parent_report_id:
                raise ValueError("changed parent Finding must reference the bound report")
        audit_ids = [item.audit_id for item in self.audit]
        if len(audit_ids) != len(set(audit_ids)):
            raise ValueError("version difference audit IDs must be unique")
        old_ids = [item.finding_id for item in self.unchanged_previous_findings]
        changed_ids = [
            item.previous_finding.finding_id
            for item in self.version_diff.entries
            if item.previous_finding is not None
        ]
        if len(old_ids) != len(set(old_ids)) or len(changed_ids) != len(set(changed_ids)):
            raise ValueError("a parent Finding may occur only once")
        if set(old_ids).intersection(changed_ids):
            raise ValueError("changed and unchanged parent Findings must be disjoint")
        if any(item.report_id != self.parent_report_id for item in self.unchanged_previous_findings):
            raise ValueError("unchanged parent Finding must reference the bound report")
        consumed = {
            (item.origin_stage, item.ruleset_version, item.finding_id)
            for item in self.consumed_new_findings
        }
        new = {(item.origin_stage, item.ruleset_version, item.finding_id) for item in self.new_findings}
        if consumed.intersection(new):
            raise ValueError("consumed and new Findings must be disjoint")
        if any(item.change_type == "replaced" for item in self.version_diff.entries):
            raise ValueError("replaced is not supported by this ruleset")
        reject_nonfinite(self)
        return self
