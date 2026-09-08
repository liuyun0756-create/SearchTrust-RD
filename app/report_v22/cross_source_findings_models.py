"""Strict internal contracts for deterministic V22-071 cross-source Findings."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from app.jobs_v22.digest import request_digest
from app.report_v22.evidence_models import EvidenceBuildResult, reject_nonfinite
from app.report_v22.first_party_findings_models import FirstPartyFindingsInput, FirstPartyFindingsResult
from app.report_v22.models import EvidenceId, Finding, FindingId, StrictModel


CrossSourcePair = Literal["gsc_ga4", "gsc_gbp", "ga4_gbp"]
CrossSourceKind = Literal["business", "measurement"]
CrossSourceState = Literal["triggered", "not_triggered", "not_checked"]
CrossSourceReason = Literal[
    "condition_met", "condition_not_met", "source_missing", "source_unhealthy",
    "source_expired", "comparison_unavailable", "insufficient_sample",
    "field_not_observed", "detail_truncated", "provider_limitation",
    "window_mismatch", "page_identity_ambiguous", "output_limit",
]
CrossSourceTargetKind = Literal["page", "aggregate", "weekly_series"]
PairAssessmentState = Literal["eligible_for_business", "not_checked"]

PAIR_SOURCES: dict[str, tuple[str, str]] = {
    "gsc_ga4": ("gsc", "ga4"),
    "gsc_gbp": ("gsc", "gbp"),
    "ga4_gbp": ("ga4", "gbp"),
}


class CrossSourceFindingsLimits(StrictModel):
    max_page_business_findings_per_rule: int = Field(default=5, ge=1, le=5)
    max_aggregate_business_findings_per_pair: int = Field(default=2, ge=1, le=2)
    max_measurement_findings: int = Field(default=5, ge=1, le=5)
    max_findings: int = Field(default=25, ge=1, le=25)
    max_evaluations: int = Field(default=5000, ge=1, le=5000)
    max_evidence_items: int = Field(default=10000, ge=1, le=10000)
    max_bytes: int = Field(default=20_000_000, ge=1, le=20_000_000)


class CrossSourceFindingsInput(StrictModel):
    case_id: UUID
    parent_report_id: UUID
    evaluated_at: AwareDatetime
    normalized_domain: str = Field(min_length=1, max_length=253, pattern=r"^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$")
    first_party_input: FirstPartyFindingsInput
    first_party_result: FirstPartyFindingsResult
    first_party_input_checksum: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    first_party_result_checksum: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    limits: CrossSourceFindingsLimits = Field(default_factory=CrossSourceFindingsLimits)

    @model_validator(mode="after")
    def validate_binding(self) -> "CrossSourceFindingsInput":
        if (
            self.first_party_input.case_id != self.case_id
            or self.first_party_input.parent_report_id != self.parent_report_id
            or self.first_party_input.evaluated_at != self.evaluated_at
        ):
            raise ValueError("first-party input binding mismatch")
        if request_digest(self.first_party_input) != self.first_party_input_checksum:
            raise ValueError("first-party input checksum mismatch")
        if request_digest(self.first_party_result) != self.first_party_result_checksum:
            raise ValueError("first-party result checksum mismatch")
        reject_nonfinite(self)
        return self


class CrossSourceTarget(StrictModel):
    pair: CrossSourcePair
    kind: CrossSourceTargetKind
    key: str = Field(min_length=1, max_length=500)


class CrossSourceRuleEvaluation(StrictModel):
    rule_id: str = Field(min_length=1, max_length=120)
    rule_version: str = Field(min_length=1, max_length=64)
    finding_kind: CrossSourceKind
    target: CrossSourceTarget
    state: CrossSourceState
    reason: CrossSourceReason
    evidence_ids: list[EvidenceId] = Field(default_factory=list)
    comparator_ids: list[EvidenceId] = Field(default_factory=list)
    finding_id: FindingId | None = None
    impact_score: float = Field(default=0, ge=0)
    limitations: list[str] = Field(default_factory=list, max_length=40)

    @model_validator(mode="after")
    def validate_trigger(self) -> "CrossSourceRuleEvaluation":
        if (self.state == "triggered") != (self.finding_id is not None):
            raise ValueError("only triggered evaluations reference a Finding")
        if len(self.evidence_ids) != len(set(self.evidence_ids)) or len(self.comparator_ids) != len(set(self.comparator_ids)):
            raise ValueError("duplicate evidence reference")
        return self


class CrossSourcePairAssessment(StrictModel):
    pair: CrossSourcePair
    source_types: tuple[Literal["gsc", "ga4", "gbp"], Literal["gsc", "ga4", "gbp"]]
    snapshot_ids: tuple[UUID | None, UUID | None]
    state: PairAssessmentState
    reasons: list[str] = Field(default_factory=list, max_length=40)
    limitations: list[str] = Field(default_factory=list, max_length=40)

    @model_validator(mode="after")
    def validate_pair(self) -> "CrossSourcePairAssessment":
        if self.source_types != PAIR_SOURCES[self.pair]:
            raise ValueError("pair sources are not canonical")
        if self.state == "eligible_for_business" and any(value is None for value in self.snapshot_ids):
            raise ValueError("eligible pair requires both snapshots")
        return self


class CrossSourceFindingsResult(StrictModel):
    ruleset_version: Literal["v22_cross_source_findings_v1"] = "v22_cross_source_findings_v1"
    first_party_input_checksum: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    first_party_result_checksum: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    evidence_result: EvidenceBuildResult
    findings: list[Finding] = Field(max_length=25)
    rule_evaluations: list[CrossSourceRuleEvaluation] = Field(max_length=5000)
    pair_assessments: list[CrossSourcePairAssessment] = Field(min_length=3, max_length=3)

    @model_validator(mode="after")
    def validate_result(self) -> "CrossSourceFindingsResult":
        if [item.pair for item in self.pair_assessments] != ["gsc_ga4", "gsc_gbp", "ga4_gbp"]:
            raise ValueError("pair assessments must use canonical order")
        finding_by_id = {item.finding_id: item for item in self.findings}
        if len(finding_by_id) != len(self.findings):
            raise ValueError("duplicate Finding IDs")
        evidence_by_id = {item.evidence_id: item for item in self.evidence_result.evidence_index}
        trace_ids = {item.evidence_id for item in self.evidence_result.source_traces}
        if set(evidence_by_id) != trace_ids:
            raise ValueError("evidence and trace identities differ")
        eligible = {item.pair: item.state == "eligible_for_business" for item in self.pair_assessments}
        triggered: list[str] = []
        for evaluation in self.rule_evaluations:
            references = evaluation.evidence_ids + evaluation.comparator_ids
            if not set(references) <= set(evidence_by_id):
                raise ValueError("evaluation references unknown evidence")
            if evaluation.finding_id is None:
                continue
            finding = finding_by_id.get(evaluation.finding_id)
            if finding is None or (
                finding.rule_id != evaluation.rule_id
                or finding.rule_version != evaluation.rule_version
                or finding.evidence_ids != evaluation.evidence_ids
                or finding.comparator_ids != evaluation.comparator_ids
            ):
                raise ValueError("evaluation and Finding references differ")
            if {evidence_by_id[key].source_type for key in references} != set(PAIR_SOURCES[evaluation.target.pair]):
                raise ValueError("triggered Finding must reference its exact source pair")
            if evaluation.finding_kind == "business" and not eligible[evaluation.target.pair]:
                raise ValueError("business Finding requires an eligible pair")
            if evaluation.finding_kind == "business" and any(evidence_by_id[key].health_status != "healthy" for key in references):
                raise ValueError("business Finding evidence must be healthy")
            triggered.append(evaluation.finding_id)
        if len(triggered) != len(set(triggered)) or set(triggered) != set(finding_by_id):
            raise ValueError("Findings and triggered evaluations must be one-to-one")
        return self
