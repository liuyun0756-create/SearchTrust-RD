"""Pure deterministic entry point for V22-070 single-source Findings."""

from __future__ import annotations

from collections import defaultdict

from pydantic import ValidationError

from app.google_connections_v22.ga4 import Ga4Snapshot, evaluate_health as evaluate_ga4_health
from app.google_connections_v22.gbp import evaluate_health as evaluate_gbp_health
from app.google_connections_v22.gsc import GscSnapshot, SyncError, evaluate_health as evaluate_gsc_health
from app.jobs_v22.digest import canonical_json_bytes, request_digest
from app.report_v22 import first_party_ga4_findings, first_party_gbp_findings, first_party_gsc_findings
from app.report_v22.first_party_findings_common import FirstPartyEvidenceLedger, ProposedOutcome, outcome
from app.report_v22.first_party_findings_errors import FirstPartyFindingsError
from app.report_v22.first_party_findings_models import (
    FirstPartyFindingTarget,
    FirstPartyFindingsInput,
    FirstPartyFindingsResult,
    FirstPartySourceAssessment,
)


SOURCE_ORDER = {"gsc": 0, "ga4": 1, "gbp": 2}
SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
RULE_ORDER = {
    first_party_gsc_findings.QUERY_OPPORTUNITY: 0,
    first_party_gsc_findings.PAGE_OPPORTUNITY: 1,
    first_party_gsc_findings.DIMENSION_DECLINE: 2,
    first_party_gsc_findings.DIMENSION_GROWTH: 3,
    first_party_gsc_findings.MEASUREMENT: 4,
    first_party_ga4_findings.ENGAGEMENT_GAP: 0,
    first_party_ga4_findings.CONVERSION_GAP: 1,
    first_party_ga4_findings.PAGE_CHANGE: 2,
    first_party_ga4_findings.MEASUREMENT: 3,
    first_party_gbp_findings.IMPRESSION_CHANGE: 0,
    first_party_gbp_findings.ACTION_CHANGE: 1,
    first_party_gbp_findings.SEARCH_DEMAND: 2,
    first_party_gbp_findings.MEASUREMENT: 3,
}


def _rule_priority(rule_id: str) -> int:
    matches = [priority for prefix, priority in RULE_ORDER.items()
        if rule_id == prefix or rule_id.startswith(f"{prefix}.")]
    return min(matches) if matches else 100


def _assessment(request: FirstPartyFindingsInput, source: str) -> FirstPartySourceAssessment:
    snapshot = next((item for item in request.snapshots if item.source_type == source), None)
    if snapshot is None:
        return FirstPartySourceAssessment(source_type=source, state="not_checked", health_status="not_checked",
            reasons=["SOURCE_MISSING"])
    reasons = list(snapshot.health_reasons)
    if request.evaluated_at >= snapshot.expires_at:
        return FirstPartySourceAssessment(source_type=source, snapshot_id=snapshot.snapshot_id,
            state="not_checked", health_status="expired", reasons=sorted(set([*reasons, "SOURCE_EXPIRED"])))
    if snapshot.identity_match_status != "matched":
        return FirstPartySourceAssessment(source_type=source, snapshot_id=snapshot.snapshot_id,
            state="not_checked", health_status=snapshot.health_status,
            reasons=sorted(set([*reasons, "SOURCE_IDENTITY_NOT_MATCHED"])))
    if snapshot.health_status == "healthy":
        return FirstPartySourceAssessment(source_type=source, snapshot_id=snapshot.snapshot_id,
            state="eligible_for_business", health_status="healthy", reasons=sorted(set(reasons)))
    if snapshot.health_status == "unhealthy":
        return FirstPartySourceAssessment(source_type=source, snapshot_id=snapshot.snapshot_id,
            state="configuration_only", health_status="unhealthy", reasons=sorted(set(reasons)))
    return FirstPartySourceAssessment(source_type=source, snapshot_id=snapshot.snapshot_id,
        state="not_checked", health_status=snapshot.health_status, reasons=sorted(set(reasons)))


def _validate_snapshot(snapshot):
    checksum_payload = snapshot.raw_payload if snapshot.source_type == "gbp" else snapshot.normalized_payload
    if request_digest(checksum_payload) != snapshot.payload_checksum:
        raise FirstPartyFindingsError("CHECKSUM_MISMATCH")
    try:
        if snapshot.source_type == "gsc":
            value = GscSnapshot.model_validate_json(canonical_json_bytes(snapshot.normalized_payload))
            health, reasons = evaluate_gsc_health(value)
        elif snapshot.source_type == "ga4":
            value = Ga4Snapshot.model_validate_json(canonical_json_bytes(snapshot.normalized_payload))
            health, reasons = evaluate_ga4_health(value)
        else:
            value, performance = first_party_gbp_findings.decode(snapshot)
            health, reasons = evaluate_gbp_health(value)
            if health != snapshot.health_status or reasons != snapshot.health_reasons:
                raise FirstPartyFindingsError("CHECKSUM_MISMATCH")
            if (value.resource_id != snapshot.external_resource_id
                    or value.previous.start_date != snapshot.coverage_start
                    or value.current.end_date != snapshot.coverage_end):
                raise FirstPartyFindingsError("BINDING_INVALID")
            return value, performance
    except FirstPartyFindingsError:
        raise
    except (ValidationError, TypeError, ValueError, SyncError):
        raise FirstPartyFindingsError("INPUT_INVALID") from None
    if health != snapshot.health_status or reasons != snapshot.health_reasons:
        raise FirstPartyFindingsError("CHECKSUM_MISMATCH")
    if (value.resource_id != snapshot.external_resource_id
            or value.previous.start_date != snapshot.coverage_start
            or value.current.end_date != snapshot.coverage_end):
        raise FirstPartyFindingsError("BINDING_INVALID")
    return value


def _missing_gbp(case_id) -> list[ProposedOutcome]:
    return [outcome(case_id=case_id, rule_id=rule_id, finding_kind=kind,
        target=FirstPartyFindingTarget(source_type="gbp", kind="aggregate", key=key),
        state="not_checked", reason="source_missing", limitations=["OFFICIAL_GBP_NOT_CONNECTED"])
        for rule_id, kind, key in (
            (first_party_gbp_findings.IMPRESSION_CHANGE, "business", "impressions"),
            (first_party_gbp_findings.ACTION_CHANGE, "business", "customer_actions"),
            (first_party_gbp_findings.SEARCH_DEMAND, "business", "search_demand"),
            (first_party_gbp_findings.MEASUREMENT, "measurement", "profile_and_measurement"),
        )]


def _apply_caps(proposals: list[ProposedOutcome], request: FirstPartyFindingsInput) -> list[ProposedOutcome]:
    grouped: dict[tuple[str, str], list[ProposedOutcome]] = defaultdict(list)
    for proposed in proposals:
        if proposed.record is not None:
            key = (proposed.evaluation.target.source_type, proposed.evaluation.finding_kind)
            grouped[key].append(proposed)
    retained: set[str] = set()
    for (source, kind), values in grouped.items():
        maximum = (request.limits.max_business_findings_per_source if kind == "business"
            else request.limits.max_measurement_findings_per_source)
        ordered = sorted(values, key=lambda item: (
            -item.record.impact_score,
            item.evaluation.rule_id,
            item.evaluation.target.kind,
            item.evaluation.target.key.casefold(),
            item.record.finding.finding_id,
        ))
        retained.update(item.record.finding.finding_id for item in ordered[:maximum])

    result = []
    for proposed in proposals:
        if proposed.record is None or proposed.record.finding.finding_id in retained:
            result.append(proposed)
            continue
        result.append(ProposedOutcome(evaluation=proposed.evaluation.model_copy(update={
            "state": "not_checked",
            "reason": "output_limit",
            "finding_id": None,
            "limitations": sorted(set([*proposed.evaluation.limitations, "FINDING_OUTPUT_LIMIT"])),
        })))
    return result


def _validate_references(proposals, ledger, assessments) -> None:
    items = ledger.items
    assessment = {item.source_type: item for item in assessments}
    keys: dict[bytes, ProposedOutcome] = {}
    finding_content: dict[str, bytes] = {}
    for proposed in proposals:
        evaluation = proposed.evaluation
        key = canonical_json_bytes({"rule_id": evaluation.rule_id, "target": evaluation.target.model_dump(mode="json")})
        if key in keys and canonical_json_bytes(keys[key].evaluation) != canonical_json_bytes(evaluation):
            raise FirstPartyFindingsError("ID_CONFLICT")
        keys[key] = proposed
        references = evaluation.evidence_ids + evaluation.comparator_ids
        if any(identifier not in items for identifier in references):
            raise FirstPartyFindingsError("REFERENCE_INVALID")
        if any(items[identifier].source_type != evaluation.target.source_type for identifier in references):
            raise FirstPartyFindingsError("REFERENCE_INVALID")
        if proposed.record is not None:
            finding = proposed.record.finding
            if evaluation.finding_kind == "business" and assessment[evaluation.target.source_type].state != "eligible_for_business":
                raise FirstPartyFindingsError("REFERENCE_INVALID")
            if evaluation.finding_kind == "business" and any(items[key].health_status != "healthy" for key in references):
                raise FirstPartyFindingsError("REFERENCE_INVALID")
            encoded = canonical_json_bytes(finding)
            if finding.finding_id in finding_content and finding_content[finding.finding_id] != encoded:
                raise FirstPartyFindingsError("ID_CONFLICT")
            finding_content[finding.finding_id] = encoded


def build_first_party_findings(value: FirstPartyFindingsInput | dict) -> FirstPartyFindingsResult:
    try:
        raw = value.model_dump(mode="json", warnings=False) if isinstance(value, FirstPartyFindingsInput) else value
        request = FirstPartyFindingsInput.model_validate_json(canonical_json_bytes(raw))
    except (ValidationError, TypeError, ValueError):
        raise FirstPartyFindingsError("INPUT_INVALID") from None
    if len(canonical_json_bytes(request)) > request.limits.max_bytes:
        raise FirstPartyFindingsError("LIMIT_EXCEEDED")

    assessments = [_assessment(request, source) for source in ("gsc", "ga4", "gbp")]
    assessment_by_source = {item.source_type: item for item in assessments}
    ledger = FirstPartyEvidenceLedger(request.snapshots, maximum=request.limits.max_evidence_items)
    proposals: list[ProposedOutcome] = []
    for snapshot in sorted(request.snapshots, key=lambda item: SOURCE_ORDER[item.source_type]):
        parsed = _validate_snapshot(snapshot)
        assessment = assessment_by_source[snapshot.source_type]
        business = assessment.state == "eligible_for_business"
        measurement = assessment.state in {"eligible_for_business", "configuration_only"}
        reason = "source_expired" if assessment.health_status == "expired" else "source_unhealthy"
        if snapshot.source_type == "gsc":
            proposals.extend(first_party_gsc_findings.evaluate(case_id=request.case_id, snapshot=snapshot,
                value=parsed, ledger=ledger, business_eligible=business, measurement_eligible=measurement,
                blocked_reason=reason))
        elif snapshot.source_type == "ga4":
            proposals.extend(first_party_ga4_findings.evaluate(case_id=request.case_id, snapshot=snapshot,
                value=parsed, ledger=ledger, business_eligible=business, measurement_eligible=measurement,
                blocked_reason=reason))
        else:
            gbp, performance = parsed
            proposals.extend(first_party_gbp_findings.evaluate(case_id=request.case_id, snapshot=snapshot,
                value=gbp, performance=performance, ledger=ledger, business_eligible=business,
                measurement_eligible=measurement, blocked_reason=reason))
    if "gbp" not in {snapshot.source_type for snapshot in request.snapshots}:
        proposals.extend(_missing_gbp(request.case_id))

    if len(proposals) > request.limits.max_evaluations:
        raise FirstPartyFindingsError("LIMIT_EXCEEDED")
    proposals = _apply_caps(proposals, request)
    _validate_references(proposals, ledger, assessments)
    ordered = sorted(proposals, key=lambda item: (
        SOURCE_ORDER[item.evaluation.target.source_type], item.evaluation.rule_id,
        item.evaluation.target.kind, item.evaluation.target.key.casefold(),
    ))
    finding_records = [item.record for item in proposals if item.record is not None]
    finding_records.sort(key=lambda record: (
        SOURCE_ORDER[record.finding.rule_id.split(".")[2].lower()],
        _rule_priority(record.finding.rule_id),
        SEVERITY_ORDER[record.finding.severity],
        -record.impact_score,
        record.finding.scope.casefold(),
        record.finding.finding_id,
    ))
    findings = [record.finding for record in finding_records]
    result = FirstPartyFindingsResult(
        evidence_result=ledger.result(assessments),
        findings=findings,
        rule_evaluations=[item.evaluation for item in ordered],
        source_assessments=assessments,
    )
    if len(canonical_json_bytes(result)) > request.limits.max_bytes:
        raise FirstPartyFindingsError("LIMIT_EXCEEDED")
    return result
