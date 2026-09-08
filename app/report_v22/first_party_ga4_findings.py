"""Deterministic GA4-only behavior, conversion and measurement Findings."""

from __future__ import annotations

from app.google_connections_v22.ga4 import Ga4Snapshot
from app.report_v22.first_party_findings_common import (
    FirstPartyEvidenceLedger,
    ProposedOutcome,
    bounded_target_key,
    outcome,
    percentage,
    relative_change,
)
from app.report_v22.first_party_findings_models import FirstPartyFindingTarget, TrustedFirstPartySnapshot


ENGAGEMENT_GAP = "V22.FIRST_PARTY.GA4.ENGAGEMENT_GAP"
CONVERSION_GAP = "V22.FIRST_PARTY.GA4.CONVERSION_GAP"
PAGE_CHANGE = "V22.FIRST_PARTY.GA4.PAGE_CHANGE"
MEASUREMENT = "V22.FIRST_PARTY.GA4.MEASUREMENT_CONFIGURATION"

MEASUREMENT_REASONS = {
    "GA4_NO_CONFIGURED_KEY_EVENTS": ("GA4 has no configured key events, so conversion validation is blocked.", "high"),
    "GA4_CONFIGURED_KEY_EVENTS_NO_ACTIVITY": ("GA4 has configured key events but recorded no key-event activity in the current period.", "medium"),
    "GA4_NO_CURRENT_SESSIONS": ("GA4 returned no current sessions in the checked website scope.", "high"),
    "GA4_NO_LANDING_PAGE_ROWS": ("GA4 returned no landing-page rows in the checked website scope.", "medium"),
    "GA4_NO_ACTIVITY_DATES": ("GA4 returned no active dates in the checked period.", "high"),
    "GA4_RECENT_ACTIVITY_MISSING_REVIEW": ("Recent GA4 activity is missing and needs measurement review.", "medium"),
    "GA4_ACTIVITY_GAP_REVIEW": ("The GA4 date series contains a long activity gap that needs review.", "medium"),
}


def _target(kind: str, key: str) -> FirstPartyFindingTarget:
    return FirstPartyFindingTarget(source_type="ga4", kind=kind, key=bounded_target_key(key))


def _metric(ledger, snapshot, row, *, key, period, metric, start, end, confidence):
    return ledger.add(
        source_type="ga4",
        category="metric",
        target_key=key,
        field=metric,
        value=getattr(row, metric),
        period=period,
        metric_key=metric,
        unit="ratio" if metric in {"engagement_rate", "session_key_event_rate"} else (
            "seconds" if metric == "average_session_duration" else "count"
        ),
        confidence=confidence,
        coverage_start=start,
        coverage_end=end,
        page_path=key if len(key) <= 500 else None,
        limitations=snapshot.normalized_payload.get("limitations", []),
        origin_path=f"/{period}/landing_pages/{key}/{metric}",
    )


def _metadata(value: Ga4Snapshot):
    return [getattr(period, view).metadata for period in (value.current, value.previous)
        for view in ("totals", "landing_pages", "dates", "key_events")]


def evaluate(
    *,
    case_id,
    snapshot: TrustedFirstPartySnapshot,
    value: Ga4Snapshot,
    ledger: FirstPartyEvidenceLedger,
    business_eligible: bool,
    measurement_eligible: bool,
    blocked_reason: str,
) -> list[ProposedOutcome]:
    outcomes: list[ProposedOutcome] = []
    notes = sorted(set([*value.limitations, *snapshot.health_reasons]))
    for code in sorted(set(snapshot.health_reasons).intersection(MEASUREMENT_REASONS)) if measurement_eligible else []:
        statement, severity = MEASUREMENT_REASONS[code]
        evidence = ledger.add(source_type="ga4", category="coverage", target_key=code,
            field="health_reason", value=code, period="current", confidence="high",
            coverage_start=value.current.start_date, coverage_end=value.current.end_date,
            limitations=notes, origin_path="/health_reasons")
        outcomes.append(outcome(case_id=case_id, rule_id=f"{MEASUREMENT}.{code}", finding_kind="measurement",
            target=_target("coverage", code), state="triggered", reason="condition_met",
            evidence_ids=[evidence], statement=statement, severity=severity, confidence="high",
            impact_score=100 if severity == "high" else 50, limitations=notes,
            change_condition=f"A fresh GA4 snapshot no longer reports {code}."))

    not_set_sessions = sum(row.sessions for row in value.current.landing_pages.rows if row.landing_page == "(not set)")
    if measurement_eligible and not_set_sessions > 0:
        row = next(row for row in value.current.landing_pages.rows if row.landing_page == "(not set)")
        evidence = _metric(ledger, snapshot, row, key="(not set)", period="current", metric="sessions",
            start=value.current.start_date, end=value.current.end_date, confidence="medium")
        outcomes.append(outcome(case_id=case_id, rule_id=f"{MEASUREMENT}.GA4_LANDING_PAGE_NOT_SET",
            finding_kind="measurement", target=_target("coverage", "landing_page_not_set"),
            state="triggered", reason="condition_met", evidence_ids=[evidence],
            statement="GA4 attributed current sessions to an unresolved '(not set)' landing page.",
            severity="medium", confidence="medium", impact_score=not_set_sessions, limitations=notes,
            change_condition="A fresh GA4 snapshot contains no sessions attributed to '(not set)'."))

    metadata = _metadata(value)
    restricted_metrics = {
        restriction.metric_name
        for item in metadata
        for restriction in item.metric_restrictions
    }
    degraded = any(item.sampling or item.subject_to_thresholding or item.data_loss_from_other_row for item in metadata)
    confidence = "low" if degraded else "medium"
    if not business_eligible:
        for rule_id, key in ((ENGAGEMENT_GAP, "engagement_gaps"), (CONVERSION_GAP, "conversion_gaps"),
                             (PAGE_CHANGE, "page_changes")):
            outcomes.append(outcome(case_id=case_id, rule_id=rule_id, finding_kind="business",
                target=_target("aggregate", key), state="not_checked", reason=blocked_reason, limitations=notes))
        return outcomes

    totals = value.current.totals.rows[0] if value.current.totals.rows else None
    for row in sorted(value.current.landing_pages.rows, key=lambda item: item.landing_page.casefold()):
        key = row.landing_page
        target = _target("landing_page", key)
        if restricted_metrics.intersection({"sessions", "engagedSessions", "engagementRate"}):
            engagement_state, engagement_reason = "not_checked", "provider_limitation"
        elif totals is None or totals.engagement_rate <= 0:
            engagement_state, engagement_reason = "not_checked", "field_not_observed"
        elif row.sessions < 20:
            engagement_state, engagement_reason = "not_checked", "insufficient_sample"
        else:
            engagement_state = "triggered" if row.engagement_rate <= totals.engagement_rate * 0.8 else "not_triggered"
            engagement_reason = "condition_met" if engagement_state == "triggered" else "condition_not_met"
        evidence = comparators = []
        if engagement_state == "triggered":
            evidence = [_metric(ledger, snapshot, row, key=key, period="current", metric=metric,
                start=value.current.start_date, end=value.current.end_date, confidence=confidence)
                for metric in ("sessions", "engagement_rate")]
            comparators = [_metric(ledger, snapshot, totals, key="site_total", period="current",
                metric="engagement_rate", start=value.current.start_date, end=value.current.end_date,
                confidence=confidence)]
        outcomes.append(outcome(case_id=case_id, rule_id=ENGAGEMENT_GAP, finding_kind="business",
            target=target, state=engagement_state, reason=engagement_reason,
            evidence_ids=evidence, comparator_ids=comparators,
            statement="This landing page has at least 20 sessions and an engagement rate at least 20% below the site's current GA4 baseline."
                if engagement_state == "triggered" else None,
            severity="medium", confidence=confidence,
            impact_score=row.sessions * max(0, (totals.engagement_rate if totals else 0) - row.engagement_rate),
            limitations=notes,
            change_condition="The landing page no longer has sufficient sessions or its engagement gap is below 20%."
                if engagement_state == "triggered" else None))

        if restricted_metrics.intersection({"sessions", "keyEvents", "sessionKeyEventRate"}):
            conversion_state, conversion_reason = "not_checked", "provider_limitation"
        elif not value.configured_key_events or totals is None:
            conversion_state, conversion_reason = "not_checked", "field_not_observed"
        elif row.sessions < 20:
            conversion_state, conversion_reason = "not_checked", "insufficient_sample"
        else:
            low_relative = totals.session_key_event_rate > 0 and row.session_key_event_rate <= totals.session_key_event_rate * 0.5
            conversion_state = "triggered" if row.key_events == 0 or low_relative else "not_triggered"
            conversion_reason = "condition_met" if conversion_state == "triggered" else "condition_not_met"
        evidence = comparators = []
        if conversion_state == "triggered":
            evidence = [_metric(ledger, snapshot, row, key=key, period="current", metric=metric,
                start=value.current.start_date, end=value.current.end_date, confidence=confidence)
                for metric in ("sessions", "key_events", "session_key_event_rate")]
            if totals and totals.session_key_event_rate > 0:
                comparators = [_metric(ledger, snapshot, totals, key="site_total", period="current",
                    metric="session_key_event_rate", start=value.current.start_date, end=value.current.end_date,
                    confidence=confidence)]
        outcomes.append(outcome(case_id=case_id, rule_id=CONVERSION_GAP, finding_kind="business",
            target=target, state=conversion_state, reason=conversion_reason,
            evidence_ids=evidence, comparator_ids=comparators,
            statement="This landing page has at least 20 sessions but no observed key events or a key-event rate at least 50% below the site's current baseline."
                if conversion_state == "triggered" else None,
            severity="high" if row.sessions >= 100 else "medium", confidence=confidence,
            impact_score=row.sessions * max(0, (totals.session_key_event_rate if totals else 0) - row.session_key_event_rate),
            limitations=notes,
            change_condition="The landing page no longer has sufficient sessions or its key-event gap no longer meets this rule."
                if conversion_state == "triggered" else None))

    current = {row.landing_page: row for row in value.current.landing_pages.rows}
    previous = {row.landing_page: row for row in value.previous.landing_pages.rows}
    for key in sorted(set(current).union(previous), key=str.casefold):
        if key not in current or key not in previous:
            reason = ("detail_truncated" if value.current.landing_pages.truncated
                or value.previous.landing_pages.truncated else "comparison_unavailable")
            limitations = [*notes, "GA4_LANDING_PAGE_TRUNCATED"] if reason == "detail_truncated" else notes
            outcomes.append(outcome(case_id=case_id, rule_id=PAGE_CHANGE, finding_kind="business",
                target=_target("landing_page", key), state="not_checked", reason=reason,
                limitations=limitations))
            continue

        now, before = current[key], previous[key]
        if now.sessions < 20 or before.sessions < 20:
            outcomes.append(outcome(case_id=case_id, rule_id=PAGE_CHANGE, finding_kind="business",
                target=_target("landing_page", key), state="not_checked", reason="insufficient_sample",
                limitations=notes))
            continue

        changes = []
        candidates = (
            ("sessions", "sessions", "sessions"),
            ("engagement rate", "engagement_rate", "engagementRate"),
            ("key-event rate", "session_key_event_rate", "sessionKeyEventRate"),
        )
        for label, attribute, provider_name in candidates:
            if provider_name in restricted_metrics:
                continue
            if attribute == "session_key_event_rate" and not value.configured_key_events:
                continue
            change = relative_change(getattr(now, attribute), getattr(before, attribute))
            if change is not None:
                changes.append((label, attribute, change))

        if not changes:
            outcomes.append(outcome(case_id=case_id, rule_id=PAGE_CHANGE, finding_kind="business",
                target=_target("landing_page", key), state="not_checked",
                reason="provider_limitation" if restricted_metrics else "comparison_unavailable",
                limitations=notes))
            continue

        significant = [item for item in changes if abs(item[2]) >= 0.2]
        if not significant:
            outcomes.append(outcome(case_id=case_id, rule_id=PAGE_CHANGE, finding_kind="business",
                target=_target("landing_page", key), state="not_triggered", reason="condition_not_met",
                limitations=notes))
            continue

        for direction, selected in (
            ("increase", [item for item in significant if item[2] > 0]),
            ("decrease", [item for item in significant if item[2] < 0]),
        ):
            if not selected:
                continue
            evidence = [_metric(ledger, snapshot, now, key=key, period="current", metric=attribute,
                start=value.current.start_date, end=value.current.end_date, confidence=confidence)
                for _, attribute, _ in selected]
            comparators = [_metric(ledger, snapshot, before, key=key, period="previous", metric=attribute,
                start=value.previous.start_date, end=value.previous.end_date, confidence=confidence)
                for _, attribute, _ in selected]
            labels = ", ".join(label for label, _, _ in selected)
            largest = max(abs(change) for _, _, change in selected)
            outcomes.append(outcome(case_id=case_id, rule_id=PAGE_CHANGE, finding_kind="business",
                target=_target("landing_page", f"{key}|{direction}"), state="triggered", reason="condition_met",
                evidence_ids=evidence, comparator_ids=comparators,
                statement=f"GA4 {labels} for this landing page {direction}d by at least 20% versus the preceding 90 days.",
                severity="high" if direction == "decrease" and largest >= 0.5 else "medium",
                confidence=confidence, impact_score=largest * before.sessions,
                limitations=notes,
                change_condition="No same-direction GA4 landing-page indicator changes by at least 20% in a fresh comparison."))
    return outcomes
