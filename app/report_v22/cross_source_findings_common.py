"""Evidence and outcome helpers shared by V22-071 source-pair rules."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Iterable

from pydantic import HttpUrl

from app.report_v22.cross_source_findings_errors import CrossSourceFindingsError
from app.report_v22.cross_source_findings_identity import cross_source_finding_id
from app.report_v22.cross_source_findings_models import (
    CrossSourceKind, CrossSourceRuleEvaluation, CrossSourceTarget,
)
from app.report_v22.evidence_identity import stable_key
from app.report_v22.first_party_findings_common import FirstPartyEvidenceLedger
from app.report_v22.models import Confidence, Finding, Severity


RULE_VERSION = "1.0.0"
CAUSAL_LIMITATION = "CROSS_SOURCE_COVARIATION_DOES_NOT_ESTABLISH_CAUSATION"


class CrossSourceEvidenceLedger(FirstPartyEvidenceLedger):
    def seed(self, result) -> None:
        self.items = {item.evidence_id: item for item in result.evidence_index}
        self.traces = {item.evidence_id: item for item in result.source_traces}
        if len(self.items) > self.maximum:
            raise CrossSourceFindingsError("LIMIT_EXCEEDED")


@dataclass(frozen=True)
class CrossSourceOutcome:
    evaluation: CrossSourceRuleEvaluation
    finding: Finding | None = None


def outcome(
    *, case_id, rule_id: str, finding_kind: CrossSourceKind, target: CrossSourceTarget,
    state: str, reason: str, evidence_ids: Iterable[str] = (), comparator_ids: Iterable[str] = (),
    statement: str | None = None, severity: Severity = "medium", confidence: Confidence = "medium",
    impact_score: float = 0, affected_urls: Iterable[HttpUrl] = (), limitations: Iterable[str] = (),
    change_condition: str | None = None,
) -> CrossSourceOutcome:
    evidence = sorted(set(evidence_ids))
    comparators = sorted(set(comparator_ids))
    notes = sorted(set(limitations))
    finding = None
    identifier = None
    if state == "triggered":
        if not statement or not evidence or not comparator_ids or not change_condition:
            raise CrossSourceFindingsError("REFERENCE_INVALID")
        identifier = cross_source_finding_id(case_id=case_id, rule_id=rule_id, rule_version=RULE_VERSION,
            target=target, evidence_ids=evidence, comparator_ids=comparators)
        finding = Finding(
            finding_id=identifier, statement=statement, evidence_ids=evidence, comparator_ids=comparators,
            rule_id=rule_id, rule_version=RULE_VERSION, classification="fact", severity=severity,
            scope=f"cross:{target.pair}:{target.kind}:{stable_key(target.model_dump(mode='json'))[:24]}",
            confidence=confidence, affected_urls=sorted(set(affected_urls), key=str), missing_data=notes,
            change_conditions=[change_condition],
        )
    return CrossSourceOutcome(
        evaluation=CrossSourceRuleEvaluation(
            rule_id=rule_id, rule_version=RULE_VERSION, finding_kind=finding_kind, target=target,
            state=state, reason=reason, evidence_ids=evidence, comparator_ids=comparators,
            finding_id=identifier, impact_score=impact_score if state == "triggered" else 0,
            limitations=notes,
        ),
        finding=finding,
    )


def metric_evidence(
    ledger, snapshot, row, *, source: str, raw_key: str, canonical_key: str, period: str,
    metric: str, start: date, end: date, origin: str, confidence: Confidence = "medium",
) -> str:
    value = getattr(row, metric)
    provider_metric = {
        "engagement_rate": "engagementRate", "session_key_event_rate": "sessionKeyEventRate",
        "key_events": "keyEvents",
    }.get(metric, metric)
    return ledger.add(
        source_type=source, category="metric", target_key=f"{canonical_key}|raw:{raw_key}",
        field=metric, value=value, period=period, metric_key=provider_metric,
        unit="ratio" if metric in {"ctr", "engagement_rate", "session_key_event_rate"} else "count",
        confidence=confidence, coverage_start=start, coverage_end=end,
        page_path=raw_key if source == "ga4" and len(raw_key) <= 500 else None,
        url=HttpUrl(raw_key) if source == "gsc" and raw_key.startswith(("http://", "https://")) else None,
        limitations=snapshot.health_reasons, origin_path=origin,
    )


def aggregate_evidence(
    ledger, snapshot, row, *, source: str, target: str, period: str, metric: str,
    start: date, end: date, origin: str, confidence: Confidence = "medium",
) -> str:
    return ledger.add(
        source_type=source, category="metric", target_key=target, field=metric,
        value=getattr(row, metric), period=period, metric_key=metric,
        unit="count", confidence=confidence, coverage_start=start, coverage_end=end,
        limitations=snapshot.health_reasons, origin_path=origin,
    )


def categorical_gbp(
    ledger, snapshot, *, target: str, field: str, value: str, period: str,
    start: date, end: date, origin: str,
) -> str:
    return ledger.add(
        source_type="gbp", category="metric", target_key=target, field=field, value=value,
        period=period, metric_key=field, unit="category", confidence="high",
        coverage_start=start, coverage_end=end, limitations=snapshot.health_reasons,
        origin_path=origin,
    )


def change_band(change: float) -> str:
    direction = "increase" if change > 0 else "decrease"
    magnitude = "50_plus" if abs(change) >= .5 else "20_to_49"
    return f"{direction}_{magnitude}_percent"
