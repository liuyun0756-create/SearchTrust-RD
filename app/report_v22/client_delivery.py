"""Build the client-facing report projection from validated report facts."""

from __future__ import annotations

import re
from collections.abc import Iterable

from app.report_v22.client_delivery_catalog import get_client_delivery_template
from app.report_v22.models import (
    ClientCoverageAppendix,
    ClientDecision,
    ClientDelivery,
    ClientEvidenceCard,
    ClientPriorityAction,
    ClientRoadmapPhase,
    DataCoverage,
    EvidenceItem,
    Finding,
    ReportType,
    SourceType,
    TopAction,
)


class ClientDeliveryError(ValueError):
    """Raised when a safe, fully evidenced client projection cannot be built."""


_SOURCE_LABELS: dict[SourceType, str] = {
    "site": "Website",
    "serp": "Market search sample",
    "competitor": "Competitor sample",
    "gsc": "Search Console",
    "gbp": "Public business profile",
    "ga4": "Analytics",
    "pagespeed": "Page experience",
    "coverage": "Source coverage",
}

_ROADMAP_PERIODS = ("days_1_30", "days_31_60", "days_61_90")
_ROADMAP_RESULTS = (
    "The first priority has a documented completion check.",
    "The second priority has a documented completion check.",
    "The third priority has a documented completion check.",
)

_UNSAFE_DISPLAY_PATTERNS = (
    re.compile(r"https?://", re.IGNORECASE),
    re.compile(r"\b(?:fn|ev|ac|lim)_[a-z0-9][a-z0-9_-]*\b", re.IGNORECASE),
    re.compile(r"\bv22_[a-z0-9_.-]+\b", re.IGNORECASE),
    re.compile(r"\b\d+\.\d+\.\d+\b"),
    re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b"),
    re.compile(r"\b(?:guaranteed?|will)\s+(?:increase|improve|raise|grow)\b", re.IGNORECASE),
)


def _display_strings(delivery: ClientDelivery) -> Iterable[str]:
    yield delivery.decision.headline
    yield delivery.decision.business_impact
    yield delivery.decision.opportunity
    for card in delivery.evidence_cards:
        yield card.source_label
        if card.subject_label:
            yield card.subject_label
        yield card.observation
        yield card.decision_relevance
    for action in delivery.priority_actions:
        yield action.title
        yield action.why_now
        yield action.expected_result
        yield from action.required_client_assets
    for phase in delivery.roadmap:
        yield phase.objective
        yield phase.expected_result
    yield from delivery.coverage_appendix.checked_sources
    yield from delivery.coverage_appendix.unavailable_sources
    yield delivery.coverage_appendix.boundary_summary


def assert_client_delivery_safe(delivery: ClientDelivery) -> None:
    """Reject technical leakage and outcome promises in display copy."""

    for value in _display_strings(delivery):
        if any(pattern.search(value) for pattern in _UNSAFE_DISPLAY_PATTERNS):
            raise ClientDeliveryError("client delivery contains unsafe display copy")


def _evidence_card(
    *,
    action: TopAction,
    template_key: str,
    findings_by_id: dict[str, Finding],
    evidence_by_id: dict[str, EvidenceItem],
    used_findings: set[str],
) -> ClientEvidenceCard | None:
    template = get_client_delivery_template(template_key)
    for finding_id in action.finding_ids:
        if finding_id in used_findings:
            continue
        finding = findings_by_id.get(finding_id)
        if finding is None:
            continue
        eligible = [
            evidence_by_id[evidence_id]
            for evidence_id in finding.evidence_ids
            if evidence_id in evidence_by_id
            and evidence_by_id[evidence_id].health_status == "healthy"
        ]
        if not eligible:
            continue
        source_type = eligible[0].source_type
        source_evidence_ids = [
            item.evidence_id for item in eligible if item.source_type == source_type
        ][:2]
        used_findings.add(finding_id)
        return ClientEvidenceCard(
            source_label=_SOURCE_LABELS[source_type],
            subject_label=template.evidence_subject,
            observation=template.evidence_observation,
            decision_relevance=template.evidence_relevance,
            finding_ids=[finding.finding_id],
            evidence_ids=source_evidence_ids,
        )
    return None


def _coverage_appendix(
    report_type: ReportType,
    data_coverage: DataCoverage,
) -> ClientCoverageAppendix:
    checked: list[str] = []
    unavailable: list[str] = []
    for source in data_coverage.sources:
        label = _SOURCE_LABELS[source.source_type]
        if source.checked_items > 0 and source.health_status not in {
            "unhealthy",
            "unavailable",
            "expired",
            "error",
        }:
            if label not in checked:
                checked.append(label)
        elif source.health_status in {"unhealthy", "unavailable", "expired", "error"}:
            if label not in unavailable:
                unavailable.append(label)

    if report_type == "prospect":
        boundary = (
            "This report uses public evidence. Authorized Search Console, "
            "Analytics, and official business-profile performance data were not used."
        )
    else:
        boundary = (
            "This report uses the connected sources listed above. Unavailable sources "
            "were not treated as zero or as healthy."
        )
    return ClientCoverageAppendix(
        checked_sources=checked[:4],
        unavailable_sources=unavailable[:4],
        boundary_summary=boundary,
    )


def build_client_delivery(
    *,
    report_type: ReportType,
    template_keys: list[str],
    top_actions: list[TopAction],
    findings: list[Finding],
    evidence_index: list[EvidenceItem],
    data_coverage: DataCoverage,
) -> ClientDelivery:
    """Build a deterministic, fully traced client delivery model."""

    if len(template_keys) != 3 or len(top_actions) != 3:
        raise ClientDeliveryError("client delivery requires exactly three actions")
    if [action.sequence for action in top_actions] != [1, 2, 3]:
        raise ClientDeliveryError("client delivery actions must be ordered")

    try:
        templates = [get_client_delivery_template(key) for key in template_keys]
    except ValueError as exc:
        raise ClientDeliveryError(str(exc)) from exc

    findings_by_id = {finding.finding_id: finding for finding in findings}
    evidence_by_id = {evidence.evidence_id: evidence for evidence in evidence_index}
    used_findings: set[str] = set()
    evidence_cards = [
        card
        for action, key in zip(top_actions, template_keys, strict=True)
        if (
            card := _evidence_card(
                action=action,
                template_key=key,
                findings_by_id=findings_by_id,
                evidence_by_id=evidence_by_id,
                used_findings=used_findings,
            )
        )
        is not None
    ]
    if not evidence_cards:
        raise ClientDeliveryError("client delivery has no healthy representative evidence")

    priority_actions = [
        ClientPriorityAction(
            action_id=action.action_id,
            sequence=action.sequence,
            title=template.action_title,
            why_now=template.why_now,
            expected_result=template.expected_result,
            effort_bucket=action.effort_bucket,
            review_date=action.review_date,
            required_client_assets=action.required_client_assets[:3],
        )
        for action, template in zip(top_actions, templates, strict=True)
    ]
    roadmap = [
        ClientRoadmapPhase(
            period=period,
            objective=template.action_title,
            expected_result=result,
            action_ids=[action.action_id],
        )
        for action, template, period, result in zip(
            top_actions,
            templates,
            _ROADMAP_PERIODS,
            _ROADMAP_RESULTS,
            strict=True,
        )
    ]
    lead = templates[0]
    delivery = ClientDelivery(
        decision=ClientDecision(
            headline=lead.decision_headline,
            business_impact=lead.business_impact,
            opportunity=lead.opportunity,
        ),
        evidence_cards=evidence_cards,
        priority_actions=priority_actions,
        roadmap=roadmap,
        coverage_appendix=_coverage_appendix(report_type, data_coverage),
        next_review_date=top_actions[0].review_date,
    )
    assert_client_delivery_safe(delivery)
    return delivery
