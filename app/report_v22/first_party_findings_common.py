"""Evidence ledger and outcome helpers shared by first-party rule families."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Iterable

from pydantic import HttpUrl, TypeAdapter, ValidationError

from app.api.v2.models import DimensionValue, ProviderRequestContext
from app.report_v22.evidence_identity import evidence_id, stable_key
from app.report_v22.evidence_models import (
    EvidenceBuildResult,
    EvidenceCoverageGap,
    EvidenceObservation,
    EvidenceSelector,
    EvidenceSourceSummary,
    EvidenceSourceTrace,
)
from app.report_v22.first_party_findings_errors import FirstPartyFindingsError
from app.report_v22.first_party_findings_identity import first_party_finding_id
from app.report_v22.first_party_findings_models import (
    FirstPartyFindingKind,
    FirstPartyFindingRecord,
    FirstPartyFindingTarget,
    FirstPartyRuleEvaluation,
    FirstPartySourceType,
    TrustedFirstPartySnapshot,
)
from app.report_v22.models import Confidence, EvidenceItem, Finding, Severity, SourceLocator


RULE_VERSION = "1.0.0"
_CONFIDENCE_ORDER = {"low": 0, "medium": 1, "high": 2}
_URL = TypeAdapter(HttpUrl)


def relative_change(current: float, previous: float) -> float | None:
    if previous <= 0:
        return None
    return (current - previous) / previous


def safe_url(value: str) -> HttpUrl | None:
    try:
        return _URL.validate_python(value, strict=True)
    except ValidationError:
        return None


def percentage(value: float) -> str:
    return f"{abs(value) * 100:.1f}%"


def bounded_target_key(value: str, *, maximum: int = 500) -> str:
    if len(value) <= maximum:
        return value
    return f"hashed:{stable_key(value)}"


class FirstPartyEvidenceLedger:
    def __init__(self, snapshots: list[TrustedFirstPartySnapshot], *, maximum: int) -> None:
        self.snapshots = {snapshot.source_type: snapshot for snapshot in snapshots}
        self.maximum = maximum
        self.items: dict[str, EvidenceItem] = {}
        self.traces: dict[str, EvidenceSourceTrace] = {}

    def add(
        self,
        *,
        source_type: FirstPartySourceType,
        category: str,
        target_key: str,
        field: str,
        value: str | int | float | bool | None,
        period: str,
        metric_key: str | None = None,
        unit: str | None = None,
        confidence: Confidence = "high",
        coverage_start: date | None = None,
        coverage_end: date | None = None,
        query: str | None = None,
        page_path: str | None = None,
        url: HttpUrl | None = None,
        limitations: Iterable[str] = (),
        origin_path: str,
    ) -> str:
        snapshot = self.snapshots[source_type]
        dimensions = [DimensionValue(key="period", value=period)]
        selector = EvidenceSelector(
            category="metric" if category == "metric" else "coverage",
            record_key=stable_key({"source": source_type, "target": target_key, "period": period}),
            record_context=[source_type, period, target_key[:300]],
            field=field,
            metric_key=metric_key,
            unit=unit,
            dimensions=dimensions,
            request_context=ProviderRequestContext(
                external_resource_id=snapshot.external_resource_id,
                start_date=coverage_start.isoformat() if coverage_start else None,
                end_date=coverage_end.isoformat() if coverage_end else None,
            ),
        )
        locator = SourceLocator(
            external_resource_id=snapshot.external_resource_id,
            query=query if query is not None and len(query) <= 300 else None,
            page_path=page_path if page_path is not None and len(page_path) <= 500 else None,
            url=url,
        )
        notes = sorted(set([*snapshot.health_reasons, *limitations]))[:20]
        observation = EvidenceObservation(
            snapshot_id=snapshot.snapshot_id,
            source_type=source_type,
            selector=selector,
            source_locator=locator,
            original_value=value,
            normalized_value=value,
            collected_at=snapshot.fetched_at,
            coverage_start=coverage_start,
            coverage_end=coverage_end,
            confidence=confidence,
            health_status=snapshot.health_status,
            limitations=notes,
            origin_paths=[origin_path if len(origin_path) <= 500 else f"/hashed/{stable_key(origin_path)}"],
        )
        identifier = evidence_id(observation)
        item = EvidenceItem(
            evidence_id=identifier,
            snapshot_id=observation.snapshot_id,
            source_type=observation.source_type,
            source_locator=observation.source_locator,
            original_value=observation.original_value,
            normalized_value=observation.normalized_value,
            collected_at=observation.collected_at,
            coverage_start=observation.coverage_start,
            coverage_end=observation.coverage_end,
            confidence=observation.confidence,
            health_status=observation.health_status,
            limitations=observation.limitations,
        )
        trace = EvidenceSourceTrace(
            evidence_id=identifier,
            snapshot_id=observation.snapshot_id,
            selector=selector,
            origin_paths=observation.origin_paths,
        )
        if identifier in self.items and (self.items[identifier] != item or self.traces[identifier] != trace):
            raise FirstPartyFindingsError("ID_CONFLICT")
        self.items[identifier] = item
        self.traces[identifier] = trace
        if len(self.items) > self.maximum:
            raise FirstPartyFindingsError("LIMIT_EXCEEDED")
        return identifier

    def result(self, assessments) -> EvidenceBuildResult:
        summaries = []
        gaps = []
        for assessment in assessments:
            snapshot = self.snapshots.get(assessment.source_type)
            if snapshot is not None:
                summaries.append(EvidenceSourceSummary(
                    source_type=assessment.source_type,
                    gbp_origin="first_party" if assessment.source_type == "gbp" else None,
                    snapshot_id=snapshot.snapshot_id,
                    health_status=assessment.health_status,
                    identity_match_status=snapshot.identity_match_status,
                    business_eligible=assessment.state == "eligible_for_business",
                    evidence_count=sum(
                        item.snapshot_id == snapshot.snapshot_id for item in self.items.values()
                    ),
                    limitations=assessment.reasons,
                ))
            if snapshot is None:
                gaps.append(EvidenceCoverageGap(
                    source_type=assessment.source_type,
                    gbp_origin="first_party" if assessment.source_type == "gbp" else None,
                    reason="no_snapshot",
                ))
            elif assessment.state != "eligible_for_business":
                reason = "expired" if assessment.health_status == "expired" else (
                    "identity_mismatch" if snapshot.identity_match_status != "matched" else "unhealthy"
                )
                gaps.append(EvidenceCoverageGap(
                    source_type=assessment.source_type,
                    gbp_origin="first_party" if assessment.source_type == "gbp" else None,
                    reason=reason,
                    snapshot_id=snapshot.snapshot_id,
                ))
        return EvidenceBuildResult(
            evidence_index=[self.items[key] for key in sorted(self.items)],
            source_traces=[self.traces[key] for key in sorted(self.traces)],
            source_summaries=summaries,
            coverage_gaps=gaps,
        )


@dataclass(frozen=True)
class ProposedOutcome:
    evaluation: FirstPartyRuleEvaluation
    record: FirstPartyFindingRecord | None = None


def outcome(
    *,
    case_id,
    rule_id: str,
    finding_kind: FirstPartyFindingKind,
    target: FirstPartyFindingTarget,
    state: str,
    reason: str,
    evidence_ids: Iterable[str] = (),
    comparator_ids: Iterable[str] = (),
    statement: str | None = None,
    severity: Severity = "medium",
    confidence: Confidence = "medium",
    impact_score: float = 0,
    affected_urls: Iterable[HttpUrl] = (),
    affected_queries: Iterable[str] = (),
    limitations: Iterable[str] = (),
    change_condition: str | None = None,
) -> ProposedOutcome:
    evidence = sorted(set(evidence_ids))
    comparators = sorted(set(comparator_ids))
    notes = sorted(set(limitations))
    finding = None
    identifier = None
    if state == "triggered":
        if not statement or not evidence or not change_condition:
            raise FirstPartyFindingsError("REFERENCE_INVALID")
        identifier = first_party_finding_id(
            case_id=case_id,
            rule_id=rule_id,
            rule_version=RULE_VERSION,
            target=target,
            evidence_ids=evidence,
            comparator_ids=comparators,
        )
        finding = Finding(
            finding_id=identifier,
            statement=statement,
            evidence_ids=evidence,
            comparator_ids=comparators,
            rule_id=rule_id,
            rule_version=RULE_VERSION,
            classification="fact",
            severity=severity,
            scope=f"{target.source_type}:{target.kind}:{stable_key(target.model_dump(mode='json'))[:24]}",
            confidence=confidence,
            affected_urls=sorted(set(affected_urls), key=str),
            affected_queries=sorted(set(affected_queries), key=str.casefold),
            missing_data=notes,
            change_conditions=[change_condition],
        )
    evaluation = FirstPartyRuleEvaluation(
        rule_id=rule_id,
        rule_version=RULE_VERSION,
        finding_kind=finding_kind,
        target=target,
        state=state,
        reason=reason,
        evidence_ids=evidence,
        comparator_ids=comparators,
        finding_id=identifier,
        impact_score=impact_score,
        limitations=notes,
    )
    return ProposedOutcome(
        evaluation=evaluation,
        record=FirstPartyFindingRecord(finding_kind=finding_kind, impact_score=impact_score, finding=finding)
        if finding else None,
    )


def bounded_confidence(base: Confidence, evidence: Iterable[EvidenceItem]) -> Confidence:
    return min([base, *(item.confidence for item in evidence)], key=_CONFIDENCE_ORDER.get)
