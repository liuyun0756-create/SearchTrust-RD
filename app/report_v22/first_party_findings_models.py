"""Internal contracts for deterministic single-source first-party Findings."""

from __future__ import annotations

from datetime import date
from typing import Any, Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from app.report_v22.evidence_models import EvidenceBuildResult, reject_nonfinite
from app.report_v22.models import EvidenceId, Finding, FindingId, HealthStatus, IdentityMatchStatus, StrictModel


FirstPartySourceType = Literal["gsc", "ga4", "gbp"]
FirstPartyEvaluationState = Literal["triggered", "not_triggered", "not_checked"]
FirstPartyEvaluationReason = Literal[
    "condition_met",
    "condition_not_met",
    "source_missing",
    "source_unhealthy",
    "source_expired",
    "comparison_unavailable",
    "insufficient_sample",
    "field_not_observed",
    "detail_truncated",
    "provider_limitation",
    "output_limit",
]
FirstPartyTargetKind = Literal[
    "aggregate",
    "query",
    "page",
    "landing_page",
    "profile",
    "search_demand",
    "coverage",
]
FirstPartyFindingKind = Literal["business", "measurement"]
SourceAssessmentState = Literal["eligible_for_business", "configuration_only", "not_checked"]


class TrustedFirstPartySnapshot(StrictModel):
    snapshot_id: UUID
    case_id: UUID
    binding_id: UUID
    source_type: FirstPartySourceType
    schema_version: Literal["gsc_sync_v1", "ga4_sync_v1", "gbp_sync_v1"]
    fetched_at: AwareDatetime
    expires_at: AwareDatetime
    identity_match_status: IdentityMatchStatus
    health_status: HealthStatus
    health_reasons: list[str] = Field(default_factory=list, max_length=40)
    normalized_payload: dict[str, Any]
    raw_payload: dict[str, Any] | None = None
    payload_checksum: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    external_resource_id: str = Field(min_length=1, max_length=500)
    coverage_start: date
    coverage_end: date

    @model_validator(mode="after")
    def validate_source_shape(self) -> "TrustedFirstPartySnapshot":
        expected = {
            "gsc": "gsc_sync_v1",
            "ga4": "ga4_sync_v1",
            "gbp": "gbp_sync_v1",
        }[self.source_type]
        if self.schema_version != expected:
            raise ValueError("source schema mismatch")
        if self.coverage_start > self.coverage_end or self.expires_at <= self.fetched_at:
            raise ValueError("invalid snapshot interval")
        if self.source_type == "gbp" and self.raw_payload is None:
            raise ValueError("GBP raw content is required while evaluating")
        if self.source_type != "gbp" and self.raw_payload is not None:
            raise ValueError("only GBP may carry temporary raw content")
        reject_nonfinite(self.normalized_payload)
        reject_nonfinite(self.raw_payload)
        return self


class FirstPartyFindingsLimits(StrictModel):
    max_business_findings_per_source: int = Field(default=10, ge=1, le=10)
    max_measurement_findings_per_source: int = Field(default=5, ge=1, le=5)
    max_evaluations: int = Field(default=5000, ge=1, le=5000)
    max_evidence_items: int = Field(default=10000, ge=1, le=10000)
    max_bytes: int = Field(default=20_000_000, ge=1, le=20_000_000)


class FirstPartyFindingsInput(StrictModel):
    case_id: UUID
    parent_report_id: UUID
    evaluated_at: AwareDatetime
    snapshots: list[TrustedFirstPartySnapshot] = Field(min_length=2, max_length=3)
    limits: FirstPartyFindingsLimits = Field(default_factory=FirstPartyFindingsLimits)

    @model_validator(mode="after")
    def validate_sources(self) -> "FirstPartyFindingsInput":
        sources = [snapshot.source_type for snapshot in self.snapshots]
        if len(set(sources)) != len(sources) or not {"gsc", "ga4"}.issubset(sources):
            raise ValueError("GSC and GA4 snapshots are required exactly once")
        if len({snapshot.snapshot_id for snapshot in self.snapshots}) != len(self.snapshots):
            raise ValueError("snapshot identities must be unique")
        if len({snapshot.binding_id for snapshot in self.snapshots}) != len(self.snapshots):
            raise ValueError("binding identities must be unique")
        if any(snapshot.case_id != self.case_id for snapshot in self.snapshots):
            raise ValueError("snapshot belongs to another Case")
        if any(snapshot.fetched_at > self.evaluated_at for snapshot in self.snapshots):
            raise ValueError("snapshot cannot be evaluated before it was fetched")
        reject_nonfinite(self)
        return self


class FirstPartyFindingTarget(StrictModel):
    source_type: FirstPartySourceType
    kind: FirstPartyTargetKind
    key: str = Field(min_length=1, max_length=500)


class FirstPartyRuleEvaluation(StrictModel):
    rule_id: str = Field(min_length=1, max_length=120)
    rule_version: str = Field(min_length=1, max_length=64)
    finding_kind: FirstPartyFindingKind
    target: FirstPartyFindingTarget
    state: FirstPartyEvaluationState
    reason: FirstPartyEvaluationReason
    evidence_ids: list[EvidenceId] = Field(default_factory=list)
    comparator_ids: list[EvidenceId] = Field(default_factory=list)
    finding_id: FindingId | None = None
    impact_score: float = Field(default=0, ge=0)
    limitations: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_finding_state(self) -> "FirstPartyRuleEvaluation":
        if (self.state == "triggered") != (self.finding_id is not None):
            raise ValueError("only triggered evaluations reference a Finding")
        return self


class FirstPartySourceAssessment(StrictModel):
    source_type: FirstPartySourceType
    snapshot_id: UUID | None = None
    state: SourceAssessmentState
    health_status: HealthStatus
    reasons: list[str] = Field(default_factory=list)


class FirstPartyFindingRecord(StrictModel):
    finding_kind: FirstPartyFindingKind
    impact_score: float = Field(ge=0)
    finding: Finding


class FirstPartyFindingsResult(StrictModel):
    ruleset_version: Literal["v22_first_party_findings_v1"] = "v22_first_party_findings_v1"
    evidence_result: EvidenceBuildResult
    findings: list[Finding]
    rule_evaluations: list[FirstPartyRuleEvaluation]
    source_assessments: list[FirstPartySourceAssessment] = Field(min_length=3, max_length=3)

    @model_validator(mode="after")
    def validate_result(self) -> "FirstPartyFindingsResult":
        if [item.source_type for item in self.source_assessments] != ["gsc", "ga4", "gbp"]:
            raise ValueError("source assessments must use canonical source order")
        finding_ids = [finding.finding_id for finding in self.findings]
        if len(set(finding_ids)) != len(finding_ids):
            raise ValueError("duplicate Finding IDs")
        finding_by_id = {finding.finding_id: finding for finding in self.findings}
        known_findings = set(finding_by_id)
        known_evidence = {item.evidence_id for item in self.evidence_result.evidence_index}
        trace_evidence = {trace.evidence_id for trace in self.evidence_result.source_traces}
        if known_evidence != trace_evidence:
            raise ValueError("evidence and trace identities differ")
        for evaluation in self.rule_evaluations:
            if evaluation.finding_id is not None and evaluation.finding_id not in known_findings:
                raise ValueError("evaluation references an unknown Finding")
            if not set(evaluation.evidence_ids + evaluation.comparator_ids) <= known_evidence:
                raise ValueError("evaluation references unknown evidence")
            if evaluation.finding_id is not None:
                finding = finding_by_id[evaluation.finding_id]
                if (
                    finding.rule_id != evaluation.rule_id
                    or finding.rule_version != evaluation.rule_version
                    or finding.evidence_ids != evaluation.evidence_ids
                    or finding.comparator_ids != evaluation.comparator_ids
                ):
                    raise ValueError("evaluation and Finding references differ")
        for finding in self.findings:
            if not set(finding.evidence_ids + finding.comparator_ids) <= known_evidence:
                raise ValueError("Finding references unknown evidence")
        evaluated_findings = [evaluation.finding_id for evaluation in self.rule_evaluations
            if evaluation.finding_id is not None]
        if len(evaluated_findings) != len(set(evaluated_findings)) or set(evaluated_findings) != known_findings:
            raise ValueError("Findings and triggered evaluations must be one-to-one")
        return self
