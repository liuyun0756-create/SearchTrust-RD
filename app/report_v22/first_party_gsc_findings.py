"""Deterministic GSC-only opportunities, trends and measurement Findings."""

from __future__ import annotations

from app.google_connections_v22.gsc import GscSnapshot
from app.report_v22.first_party_findings_common import (
    FirstPartyEvidenceLedger,
    ProposedOutcome,
    bounded_target_key,
    outcome,
    percentage,
    relative_change,
    safe_url,
)
from app.report_v22.first_party_findings_models import FirstPartyFindingTarget, TrustedFirstPartySnapshot


QUERY_OPPORTUNITY = "V22.FIRST_PARTY.GSC.QUERY_OPPORTUNITY"
PAGE_OPPORTUNITY = "V22.FIRST_PARTY.GSC.PAGE_OPPORTUNITY"
DIMENSION_DECLINE = "V22.FIRST_PARTY.GSC.DIMENSION_DECLINE"
DIMENSION_GROWTH = "V22.FIRST_PARTY.GSC.DIMENSION_GROWTH"
MEASUREMENT = "V22.FIRST_PARTY.GSC.MEASUREMENT_COVERAGE"

MEASUREMENT_REASONS = {
    "GSC_NO_CURRENT_DATA": ("No current Search Console impressions were observed in the checked period.", "high"),
    "GSC_NO_QUERY_ROWS": ("Search Console returned no query rows in the checked scope.", "medium"),
    "GSC_NO_PAGE_ROWS": ("Search Console returned no page rows in the checked scope.", "medium"),
    "GSC_NO_ACTIVITY_DATES": ("Search Console returned no active dates in the checked period.", "high"),
    "GSC_RECENT_ACTIVITY_MISSING_REVIEW": ("Recent Search Console activity is missing and needs coverage review.", "medium"),
    "GSC_ACTIVITY_GAP_REVIEW": ("The Search Console date series contains a long activity gap that needs review.", "medium"),
}


def _target(kind: str, key: str) -> FirstPartyFindingTarget:
    return FirstPartyFindingTarget(source_type="gsc", kind=kind, key=bounded_target_key(key))


def _blocked(case_id, rule_id: str, kind: str, reason: str, notes) -> ProposedOutcome:
    return outcome(
        case_id=case_id,
        rule_id=rule_id,
        finding_kind="business",
        target=_target("aggregate", kind),
        state="not_checked",
        reason=reason,
        limitations=notes,
    )


def _metric(
    ledger: FirstPartyEvidenceLedger,
    snapshot: TrustedFirstPartySnapshot,
    row,
    *,
    key: str,
    period: str,
    metric: str,
    kind: str,
    start,
    end,
) -> str:
    value = getattr(row, metric)
    locator = safe_url(key) if kind == "page" else None
    return ledger.add(
        source_type="gsc",
        category="metric",
        target_key=key,
        field=metric,
        value=value,
        period=period,
        metric_key=metric,
        unit="ratio" if metric == "ctr" else ("position" if metric == "position" else "count"),
        confidence="medium",
        coverage_start=start,
        coverage_end=end,
        query=key if kind == "query" else None,
        url=locator,
        limitations=snapshot.normalized_payload.get("limitations", []),
        origin_path=f"/{period}/{kind}s/{key}/{metric}",
    )


def evaluate(
    *,
    case_id,
    snapshot: TrustedFirstPartySnapshot,
    value: GscSnapshot,
    ledger: FirstPartyEvidenceLedger,
    business_eligible: bool,
    measurement_eligible: bool,
    blocked_reason: str,
) -> list[ProposedOutcome]:
    outcomes: list[ProposedOutcome] = []
    notes = sorted(set([*value.limitations, *snapshot.health_reasons]))

    for code in sorted(set(snapshot.health_reasons).intersection(MEASUREMENT_REASONS)) if measurement_eligible else []:
        statement, severity = MEASUREMENT_REASONS[code]
        evidence = ledger.add(
            source_type="gsc",
            category="coverage",
            target_key=code,
            field="health_reason",
            value=code,
            period="current",
            confidence="high",
            coverage_start=value.current.start_date,
            coverage_end=value.current.end_date,
            limitations=notes,
            origin_path="/health_reasons",
        )
        outcomes.append(outcome(
            case_id=case_id,
            rule_id=f"{MEASUREMENT}.{code}",
            finding_kind="measurement",
            target=_target("coverage", code),
            state="triggered",
            reason="condition_met",
            evidence_ids=[evidence],
            statement=statement,
            severity=severity,
            confidence="high",
            impact_score=100 if severity == "high" else 50,
            limitations=notes,
            change_condition=f"A fresh GSC snapshot no longer reports {code}.",
        ))

    if not business_eligible:
        outcomes.extend([
            _blocked(case_id, QUERY_OPPORTUNITY, "query_opportunities", blocked_reason, notes),
            _blocked(case_id, PAGE_OPPORTUNITY, "page_opportunities", blocked_reason, notes),
            _blocked(case_id, DIMENSION_DECLINE, "dimension_declines", blocked_reason, notes),
            _blocked(case_id, DIMENSION_GROWTH, "dimension_growth", blocked_reason, notes),
        ])
        return outcomes

    totals = value.current.totals.rows[0] if value.current.totals.rows else None
    baseline = totals.ctr if totals is not None else None
    for kind, rows, minimum, rule_id in (
        ("query", value.current.queries.rows, 50, QUERY_OPPORTUNITY),
        ("page", value.current.pages.rows, 100, PAGE_OPPORTUNITY),
    ):
        for row in sorted(rows, key=lambda item: (item.key or "").casefold()):
            key = row.key or ""
            target = _target(kind, key)
            if baseline is None or baseline <= 0:
                outcomes.append(outcome(case_id=case_id, rule_id=rule_id, finding_kind="business",
                    target=target, state="not_checked", reason="field_not_observed", limitations=notes))
                continue
            if row.impressions < minimum:
                outcomes.append(outcome(case_id=case_id, rule_id=rule_id, finding_kind="business",
                    target=target, state="not_checked", reason="insufficient_sample", limitations=notes))
                continue
            triggered = 4 <= row.position <= 20 and row.ctr <= baseline * 0.8
            if not triggered:
                outcomes.append(outcome(case_id=case_id, rule_id=rule_id, finding_kind="business",
                    target=target, state="not_triggered", reason="condition_not_met", limitations=notes))
                continue
            evidence = [
                _metric(ledger, snapshot, row, key=key, period="current", metric=metric, kind=kind,
                    start=value.current.start_date, end=value.current.end_date)
                for metric in ("impressions", "ctr", "position")
            ]
            baseline_id = _metric(ledger, snapshot, totals, key="site_total", period="current", metric="ctr",
                kind="aggregate", start=value.current.start_date, end=value.current.end_date)
            affected_url = safe_url(key) if kind == "page" else None
            label = "query" if kind == "query" else "page"
            outcomes.append(outcome(
                case_id=case_id,
                rule_id=rule_id,
                finding_kind="business",
                target=target,
                state="triggered",
                reason="condition_met",
                evidence_ids=evidence,
                comparator_ids=[baseline_id],
                statement=(f"This {label} has meaningful Search Console visibility at an average position "
                    "between 4 and 20, while its click-through rate is at least 20% below the site's current baseline."),
                severity="high" if row.impressions >= minimum * 5 else "medium",
                confidence="medium",
                impact_score=row.impressions * max(0, baseline - row.ctr),
                affected_urls=[affected_url] if affected_url else [],
                affected_queries=[key] if kind == "query" and len(key) <= 300 else [],
                limitations=notes,
                change_condition=f"The {label}'s position or click-through rate no longer meets this rule in a fresh 90-day snapshot.",
            ))

    for kind, current_rows, previous_rows in (
        ("query", value.current.queries.rows, value.previous.queries.rows),
        ("page", value.current.pages.rows, value.previous.pages.rows),
    ):
        current = {row.key: row for row in current_rows if row.key}
        previous = {row.key: row for row in previous_rows if row.key}
        view_name = "queries" if kind == "query" else "pages"
        current_view = getattr(value.current, view_name)
        previous_view = getattr(value.previous, view_name)
        for key in sorted(set(current).union(previous), key=str.casefold):
            target = _target(kind, key)
            if key not in current or key not in previous:
                reason = "detail_truncated" if current_view.truncated or previous_view.truncated else "comparison_unavailable"
                limitations = [*notes, "GSC_DETAIL_TRUNCATED"] if reason == "detail_truncated" else notes
                for rule_id in (DIMENSION_DECLINE, DIMENSION_GROWTH):
                    outcomes.append(outcome(case_id=case_id, rule_id=rule_id, finding_kind="business",
                        target=target, state="not_checked", reason=reason, limitations=limitations))
                continue

            now, before = current[key], previous[key]
            changes = []
            if before.clicks >= 10:
                changes.append(("clicks", relative_change(now.clicks, before.clicks)))
            if before.impressions >= 50:
                changes.append(("impressions", relative_change(now.impressions, before.impressions)))
            changes = [(metric, change) for metric, change in changes if change is not None]
            declines = [(metric, change) for metric, change in changes if change <= -0.2]
            decline_metric, decline_change = min(declines, key=lambda item: item[1]) if declines else (None, None)
            click_change = next((change for metric, change in changes if metric == "clicks"), None)
            if not changes:
                decline_state, decline_reason = "not_checked", "insufficient_sample"
            else:
                decline_state = "triggered" if decline_metric else "not_triggered"
                decline_reason = "condition_met" if decline_metric else "condition_not_met"
            if before.clicks < 10 or click_change is None:
                growth_state, growth_reason = "not_checked", "insufficient_sample"
            else:
                growth_state = "triggered" if click_change >= 0.2 else "not_triggered"
                growth_reason = "condition_met" if growth_state == "triggered" else "condition_not_met"
            affected_url = safe_url(key) if kind == "page" else None
            decline_ids = decline_comparators = []
            if decline_state == "triggered":
                decline_ids = [_metric(ledger, snapshot, now, key=key, period="current",
                    metric=decline_metric, kind=kind, start=value.current.start_date, end=value.current.end_date)]
                decline_comparators = [_metric(ledger, snapshot, before, key=key, period="previous",
                    metric=decline_metric, kind=kind, start=value.previous.start_date, end=value.previous.end_date)]
            outcomes.append(outcome(
                case_id=case_id, rule_id=DIMENSION_DECLINE, finding_kind="business", target=target,
                state=decline_state, reason=decline_reason, evidence_ids=decline_ids,
                comparator_ids=decline_comparators,
                statement=f"Search Console {decline_metric} for this {kind} decreased by {percentage(decline_change or 0)} versus the preceding 90 days."
                    if decline_state == "triggered" else None,
                severity="high" if decline_change is not None and decline_change <= -0.5 else "medium",
                confidence="medium", impact_score=abs(decline_change or 0) * getattr(before, decline_metric or "clicks"),
                affected_urls=[affected_url] if affected_url else [],
                affected_queries=[key] if kind == "query" and len(key) <= 300 else [],
                limitations=notes,
                change_condition=f"The matched {kind}'s {decline_metric} decline is less than 20% in a fresh period comparison."
                    if decline_state == "triggered" else None,
            ))
            growth_ids = growth_comparators = []
            if growth_state == "triggered":
                growth_ids = [_metric(ledger, snapshot, now, key=key, period="current", metric="clicks", kind=kind,
                    start=value.current.start_date, end=value.current.end_date)]
                growth_comparators = [_metric(ledger, snapshot, before, key=key, period="previous", metric="clicks", kind=kind,
                    start=value.previous.start_date, end=value.previous.end_date)]
            outcomes.append(outcome(
                case_id=case_id, rule_id=DIMENSION_GROWTH, finding_kind="business", target=target,
                state=growth_state, reason=growth_reason, evidence_ids=growth_ids,
                comparator_ids=growth_comparators,
                statement=f"Search Console clicks for this {kind} increased by {percentage(click_change or 0)} versus the preceding 90 days."
                    if growth_state == "triggered" else None,
                severity="medium", confidence="medium", impact_score=max(0, click_change or 0) * before.clicks,
                affected_urls=[affected_url] if affected_url else [],
                affected_queries=[key] if kind == "query" and len(key) <= 300 else [],
                limitations=notes,
                change_condition=f"The matched {kind}'s click growth is less than 20% in a fresh period comparison."
                    if growth_state == "triggered" else None,
            ))
    return outcomes
