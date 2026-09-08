"""Versioned, statement-free target mapping for V22-072."""

from __future__ import annotations

import unicodedata
from typing import Any

from app.report_v22 import (
    cross_source_findings as cross_rules,
    first_party_ga4_findings as ga4_rules,
    first_party_gbp_findings as gbp_rules,
    first_party_gsc_findings as gsc_rules,
)
from app.report_v22.cross_source_pages import normalize_ga4_page, normalize_gsc_page
from app.report_v22.verified_reprioritization_identity import relation_id
from app.report_v22.verified_reprioritization_models import FindingRelation, QualifiedFindingRef


FIRST_SUPPORTED = frozenset({
    gsc_rules.QUERY_OPPORTUNITY,
    gsc_rules.PAGE_OPPORTUNITY,
    gsc_rules.DIMENSION_DECLINE,
    ga4_rules.ENGAGEMENT_GAP,
    ga4_rules.CONVERSION_GAP,
    gbp_rules.SEARCH_DEMAND,
})
FIRST_DYNAMIC = frozenset({ga4_rules.PAGE_CHANGE, gbp_rules.IMPRESSION_CHANGE, gbp_rules.ACTION_CHANGE})
FIRST_AUDIT_PREFIXES = (gsc_rules.MEASUREMENT, ga4_rules.MEASUREMENT, gbp_rules.MEASUREMENT)
CROSS_SUPPORTED = frozenset({
    cross_rules.PAGE_OPPORTUNITY,
    cross_rules.PAGE_TREND,
    cross_rules.AGGREGATE_TREND,
    cross_rules.GSC_GBP_TREND,
    cross_rules.GA4_GBP_TREND,
})
CROSS_MEASUREMENT = frozenset({
    cross_rules.PAGE_CONFLICT,
    cross_rules.AGGREGATE_CONFLICT,
    cross_rules.WEEKLY_CONFLICT,
    cross_rules.GSC_GBP_CONFLICT,
    cross_rules.GA4_GBP_CONFLICT,
})
CROSS_AUDIT = frozenset({cross_rules.WEEKLY_MOVEMENT})
HARD_PUBLIC_TEMPLATES = frozenset({"restore_site_access_indexing", "align_public_gbp"})


def normalize_query(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def _public_targets(candidate: Any, normalized_domain: str) -> tuple[set[str], set[str]]:
    pages: set[str] = set()
    queries: set[str] = set()
    for target in candidate.targets:
        if target.kind == "url" and target.url is not None:
            normalized = normalize_gsc_page(str(target.url), normalized_domain)
            if normalized is not None:
                pages.add(normalized)
        elif target.kind == "query" and target.query is not None:
            queries.add(normalize_query(target.query))
    return pages, queries


def _first_relation_kind(evaluation: Any, evidence_by_id: dict[str, Any]) -> tuple[str, str]:
    rule = evaluation.rule_id
    if rule in FIRST_SUPPORTED:
        return "supports", "STRICT_SINGLE_SOURCE_SUPPORT"
    if rule == gsc_rules.DIMENSION_GROWTH:
        return "reduces_urgency", "STRICT_SINGLE_SOURCE_GROWTH"
    if rule == ga4_rules.PAGE_CHANGE:
        direction = evaluation.target.key.rsplit("|", 1)[-1]
        if direction == "increase":
            return "reduces_urgency", "STRICT_SINGLE_SOURCE_GROWTH"
        if direction == "decrease":
            return "supports", "STRICT_SINGLE_SOURCE_SUPPORT"
    if rule in {gbp_rules.IMPRESSION_CHANGE, gbp_rules.ACTION_CHANGE}:
        values = [
            str(evidence_by_id[key].normalized_value)
            for key in evaluation.evidence_ids
            if key in evidence_by_id
        ]
        if any(value.startswith("increase_") for value in values):
            return "reduces_urgency", "STRICT_SINGLE_SOURCE_GROWTH"
        if any(value.startswith("decrease_") for value in values):
            return "supports", "STRICT_SINGLE_SOURCE_SUPPORT"
    if any(rule == prefix or rule.startswith(f"{prefix}.") for prefix in FIRST_AUDIT_PREFIXES):
        return "unmatched", "AUDIT_ONLY_RULE"
    raise KeyError(rule)


def _cross_direction(evaluation: Any, evidence_by_id: dict[str, Any]) -> str | None:
    by_source: dict[str, list[Any]] = {}
    for current_id in evaluation.evidence_ids:
        item = evidence_by_id.get(current_id)
        if item is not None:
            by_source.setdefault(item.source_type, [None, None])[0] = item.normalized_value
    for previous_id in evaluation.comparator_ids:
        item = evidence_by_id.get(previous_id)
        if item is not None:
            by_source.setdefault(item.source_type, [None, None])[1] = item.normalized_value
    directions: set[str] = set()
    for current, previous in by_source.values():
        if isinstance(current, str) and current.startswith(("increase_", "decrease_")):
            directions.add("increase" if current.startswith("increase_") else "decrease")
        elif isinstance(current, (int, float)) and isinstance(previous, (int, float)):
            if current > previous:
                directions.add("increase")
            elif current < previous:
                directions.add("decrease")
    return next(iter(directions)) if len(directions) == 1 else None


def _strict_matches(candidate: Any, *, kind: str, target_key: str, normalized_domain: str) -> bool:
    pages, queries = _public_targets(candidate, normalized_domain)
    if kind in {"page", "landing_page"}:
        raw = target_key.rsplit("|", 1)[0] if kind == "landing_page" else target_key
        normalized = (
            normalize_ga4_page(raw, normalized_domain)
            if kind == "landing_page"
            else normalize_gsc_page(raw, normalized_domain)
        )
        return normalized is not None and normalized in pages
    if kind == "query":
        return candidate.template.key == "review_market_visibility" and normalize_query(target_key) in queries
    if kind in {"aggregate", "search_demand"}:
        return candidate.template.key == "review_market_visibility"
    return False


def build_business_relations(
    *, candidates: list[Any], first_party_result: Any, cross_source_result: Any,
    normalized_domain: str,
) -> list[FindingRelation]:
    relations: list[FindingRelation] = []
    first_evidence = {item.evidence_id: item for item in first_party_result.evidence_result.evidence_index}
    cross_evidence = {item.evidence_id: item for item in cross_source_result.evidence_result.evidence_index}

    def append(ref: QualifiedFindingRef, relation_kind: str, reason: str, target: str, candidate: Any | None) -> None:
        if candidate is not None and relation_kind == "reduces_urgency" and candidate.template.key in HARD_PUBLIC_TEMPLATES:
            relation_kind, reason, candidate = "unmatched", "HARD_PUBLIC_FACT_PROTECTED", None
        candidate_key = candidate.candidate_key if candidate is not None else None
        relations.append(FindingRelation(
            relation_id=relation_id(
                finding_ref=ref, relation_kind=relation_kind, candidate_key=candidate_key,
            ),
            finding_ref=ref,
            relation_kind=relation_kind,
            candidate_key=candidate_key,
            action_id=candidate.action_id if candidate is not None else None,
            target_key=target,
            reason_code=reason,
        ))

    for evaluation in sorted(first_party_result.rule_evaluations, key=lambda item: (item.rule_id, item.target.kind, item.target.key)):
        if evaluation.finding_id is None:
            continue
        ref = QualifiedFindingRef(
            origin_stage="first_party_findings",
            ruleset_version=first_party_result.ruleset_version,
            finding_id=evaluation.finding_id,
        )
        try:
            relation_kind, reason = _first_relation_kind(evaluation, first_evidence)
        except KeyError:
            raise
        if relation_kind == "unmatched":
            append(ref, "unmatched", reason, evaluation.target.key, None)
            continue
        matches = [candidate for candidate in candidates if _strict_matches(
            candidate, kind=evaluation.target.kind, target_key=evaluation.target.key,
            normalized_domain=normalized_domain,
        )]
        if not matches:
            append(ref, "unmatched", "NO_STRICT_TARGET_MATCH", evaluation.target.key, None)
        for candidate in matches:
            append(ref, relation_kind, reason, evaluation.target.key, candidate)

    for evaluation in sorted(cross_source_result.rule_evaluations, key=lambda item: (item.rule_id, item.target.kind, item.target.key)):
        if evaluation.finding_id is None:
            continue
        ref = QualifiedFindingRef(
            origin_stage="cross_source_findings",
            ruleset_version=cross_source_result.ruleset_version,
            finding_id=evaluation.finding_id,
        )
        if evaluation.rule_id in CROSS_MEASUREMENT:
            continue
        if evaluation.rule_id in CROSS_AUDIT:
            append(ref, "unmatched", "AUDIT_ONLY_RULE", evaluation.target.key, None)
            continue
        if evaluation.rule_id not in CROSS_SUPPORTED:
            raise KeyError(evaluation.rule_id)
        relation_kind = "supports"
        reason = "STRICT_CROSS_SOURCE_SUPPORT"
        if evaluation.rule_id != cross_rules.PAGE_OPPORTUNITY:
            direction = _cross_direction(evaluation, cross_evidence)
            if direction == "increase":
                relation_kind, reason = "reduces_urgency", "STRICT_CROSS_SOURCE_GROWTH"
            elif direction != "decrease":
                append(ref, "unmatched", "DIRECTION_NOT_PROVABLE", evaluation.target.key, None)
                continue
        matches = [candidate for candidate in candidates if _strict_matches(
            candidate, kind=evaluation.target.kind, target_key=evaluation.target.key,
            normalized_domain=normalized_domain,
        )]
        if not matches:
            append(ref, "unmatched", "NO_STRICT_TARGET_MATCH", evaluation.target.key, None)
        for candidate in matches:
            append(ref, relation_kind, reason, evaluation.target.key, candidate)

    unique: dict[str, FindingRelation] = {}
    for item in relations:
        previous = unique.get(item.relation_id)
        if previous is not None and previous != item:
            raise ValueError("conflicting relation identity")
        unique[item.relation_id] = item
    return [unique[key] for key in sorted(unique)]
