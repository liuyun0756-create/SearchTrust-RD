"""Pure deterministic entry point for V22-071 cross-source Findings."""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Iterable
from urllib.parse import urlsplit

from pydantic import HttpUrl, ValidationError

from app.google_connections_v22.ga4 import Ga4Snapshot
from app.google_connections_v22.gsc import GscSnapshot
from app.jobs_v22.digest import canonical_json_bytes, request_digest
from app.report_v22 import first_party_gbp_findings
from app.report_v22.cross_source_findings_common import (
    CAUSAL_LIMITATION, CrossSourceEvidenceLedger, CrossSourceOutcome, aggregate_evidence,
    categorical_gbp, change_band, metric_evidence, outcome,
)
from app.report_v22.cross_source_findings_errors import CrossSourceFindingsError
from app.report_v22.cross_source_findings_models import (
    PAIR_SOURCES, CrossSourceFindingsInput, CrossSourceFindingsResult,
    CrossSourcePairAssessment, CrossSourceTarget,
)
from app.report_v22.cross_source_pages import (
    consolidate_pages, normalize_ga4_page, normalize_gsc_page,
)
from app.report_v22.cross_source_time import complete_week_pairs, spearman, windows_compatible
from app.report_v22.first_party_findings import _validate_snapshot, build_first_party_findings
from app.report_v22.first_party_findings_common import bounded_target_key, relative_change


PAGE_TREND = "V22.CROSS_SOURCE.GSC_GA4.PAGE_TREND"
PAGE_OPPORTUNITY = "V22.CROSS_SOURCE.GSC_GA4.CONFIRMED_SEARCH_OPPORTUNITY"
PAGE_CONFLICT = "V22.CROSS_SOURCE.GSC_GA4.PAGE_DIRECTION_CONFLICT"
AGGREGATE_TREND = "V22.CROSS_SOURCE.GSC_GA4.AGGREGATE_TREND"
AGGREGATE_CONFLICT = "V22.CROSS_SOURCE.GSC_GA4.AGGREGATE_DIRECTION_CONFLICT"
WEEKLY_MOVEMENT = "V22.CROSS_SOURCE.GSC_GA4.WEEKLY_COVARIATION"
WEEKLY_CONFLICT = "V22.CROSS_SOURCE.GSC_GA4.WEEKLY_CONFLICT"
GSC_GBP_TREND = "V22.CROSS_SOURCE.GSC_GBP.VISIBILITY_TREND"
GSC_GBP_CONFLICT = "V22.CROSS_SOURCE.GSC_GBP.VISIBILITY_CONFLICT"
GA4_GBP_TREND = "V22.CROSS_SOURCE.GA4_GBP.BEHAVIOR_TREND"
GA4_GBP_CONFLICT = "V22.CROSS_SOURCE.GA4_GBP.BEHAVIOR_CONFLICT"

PAIR_ORDER = {"gsc_ga4": 0, "gsc_gbp": 1, "ga4_gbp": 2}
RULE_ORDER = {
    PAGE_OPPORTUNITY: 0, PAGE_TREND: 1, AGGREGATE_TREND: 2, WEEKLY_MOVEMENT: 3,
    GSC_GBP_TREND: 4, GA4_GBP_TREND: 5, PAGE_CONFLICT: 10,
    AGGREGATE_CONFLICT: 11, WEEKLY_CONFLICT: 12, GSC_GBP_CONFLICT: 13,
    GA4_GBP_CONFLICT: 14,
}
SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def semantic_first_party_input_checksum(value) -> str:
    """Bind the same source set independent of caller traversal order."""
    payload = value.model_dump(mode="json")
    payload["snapshots"] = sorted(payload["snapshots"], key=lambda item: item["source_type"])
    return request_digest(payload)


def _domain(value: str) -> str:
    value = value.rstrip(".").lower()
    return value[4:] if value.startswith("www.") else value


def _validate_domain(request, parsed) -> None:
    ga4: Ga4Snapshot = parsed["ga4"]
    if _domain(request.normalized_domain) not in {_domain(host) for host in ga4.host_filter}:
        raise CrossSourceFindingsError("BINDING_INVALID")
    gsc: GscSnapshot = parsed["gsc"]
    resource = gsc.resource_id
    if resource.startswith("sc-domain:"):
        host = resource.removeprefix("sc-domain:")
    else:
        try:
            parsed_resource = urlsplit(resource)
            host = parsed_resource.hostname or ""
            if parsed_resource.path not in {"", "/"} or parsed_resource.query or parsed_resource.fragment:
                raise CrossSourceFindingsError("BINDING_INVALID")
        except ValueError:
            host = ""
    if _domain(host) != _domain(request.normalized_domain):
        raise CrossSourceFindingsError("BINDING_INVALID")


def _third_limit(pair: str, assessments) -> list[str]:
    third = next(source for source in ("gsc", "ga4", "gbp") if source not in PAIR_SOURCES[pair])
    assessment = assessments[third]
    if assessment.state == "eligible_for_business":
        return []
    return [f"THIRD_SOURCE_{third.upper()}_{'MISSING' if assessment.snapshot_id is None else 'UNAVAILABLE'}"]


def _pair_assessments(request, first_party, parsed) -> list[CrossSourcePairAssessment]:
    source_assessments = {item.source_type: item for item in first_party.source_assessments}
    snapshots = {item.source_type: item for item in request.first_party_input.snapshots}
    result = []
    for pair in ("gsc_ga4", "gsc_gbp", "ga4_gbp"):
        sources = PAIR_SOURCES[pair]
        selected = [source_assessments[source] for source in sources]
        reasons: list[str] = []
        if any(item.snapshot_id is None for item in selected):
            reasons.append("SOURCE_MISSING")
        for item in selected:
            if item.snapshot_id is not None and item.state != "eligible_for_business":
                reasons.append(f"{item.source_type.upper()}_{item.health_status.upper()}")
        if not reasons:
            left, right = (parsed[source] for source in sources)
            left_value = left[0] if isinstance(left, tuple) else left
            right_value = right[0] if isinstance(right, tuple) else right
            if not windows_compatible(
                left_value.current.start_date, left_value.current.end_date,
                right_value.current.start_date, right_value.current.end_date,
            ) or not windows_compatible(
                left_value.previous.start_date, left_value.previous.end_date,
                right_value.previous.start_date, right_value.previous.end_date,
            ):
                reasons.append("WINDOW_MISMATCH")
        result.append(CrossSourcePairAssessment(
            pair=pair, source_types=sources,
            snapshot_ids=tuple(snapshots[source].snapshot_id if source in snapshots else None for source in sources),
            state="eligible_for_business" if not reasons else "not_checked",
            reasons=sorted(set(reasons)), limitations=_third_limit(pair, source_assessments),
        ))
    return result


def _blocked(case_id, pair: str, assessment, rules: Iterable[tuple[str, str, str, str]]) -> list[CrossSourceOutcome]:
    reason = "window_mismatch" if "WINDOW_MISMATCH" in assessment.reasons else (
        "source_missing" if "SOURCE_MISSING" in assessment.reasons else
        "source_expired" if any("EXPIRED" in item for item in assessment.reasons) else "source_unhealthy"
    )
    return [outcome(rule_id=rule, case_id=case_id, finding_kind=kind,
        target=CrossSourceTarget(pair=pair, kind=target_kind, key=key),
        state="not_checked", reason=reason, limitations=assessment.reasons + assessment.limitations)
        for rule, kind, target_kind, key in rules]


def _sum_gsc(group, metric):
    return sum(getattr(row, metric) for row in group.rows)


def _sum_ga4(group, metric):
    return sum(getattr(row, metric) for row in group.rows)


def _weighted(group, numerator: str, denominator: str) -> float:
    total = _sum_ga4(group, denominator)
    return _sum_ga4(group, numerator) / total if total else 0


def _page_refs(ledger, snapshot, group, *, source, period, metric, start, end):
    return [metric_evidence(ledger, snapshot, row, source=source, raw_key=raw,
        canonical_key=group.canonical_url, period=period, metric=metric, start=start, end=end,
        origin=f"/{period}/{'pages' if source == 'gsc' else 'landing_pages'}/{index}/{metric}")
        for index, (raw, row) in enumerate(zip(group.raw_keys, group.rows))]


def _gsc_ga4_pages(request, parsed, ledger, assessment) -> list[CrossSourceOutcome]:
    pair = "gsc_ga4"
    rules = [(PAGE_TREND, "business", "page", "page_trend"),
             (PAGE_OPPORTUNITY, "business", "page", "search_opportunity"),
             (PAGE_CONFLICT, "measurement", "page", "page_direction")]
    if assessment.state != "eligible_for_business":
        return _blocked(request.case_id, pair, assessment, rules)
    gsc: GscSnapshot = parsed["gsc"]
    ga4: Ga4Snapshot = parsed["ga4"]
    gsc_snapshot = next(item for item in request.first_party_input.snapshots if item.source_type == "gsc")
    ga4_snapshot = next(item for item in request.first_party_input.snapshots if item.source_type == "ga4")
    groups = {}
    rejected = {}
    for source, period, rows, key, normalizer in (
        ("gsc", "current", gsc.current.pages.rows, lambda row: row.key or "", normalize_gsc_page),
        ("gsc", "previous", gsc.previous.pages.rows, lambda row: row.key or "", normalize_gsc_page),
        ("ga4", "current", ga4.current.landing_pages.rows, lambda row: row.landing_page, normalize_ga4_page),
        ("ga4", "previous", ga4.previous.landing_pages.rows, lambda row: row.landing_page, normalize_ga4_page),
    ):
        groups[(source, period)], rejected[(source, period)] = consolidate_pages(
            rows, key=key, normalizer=normalizer, normalized_domain=request.normalized_domain)
    keys = sorted(set().union(*(set(value) for value in groups.values())))
    outcomes: list[CrossSourceOutcome] = []
    notes = sorted(set([CAUSAL_LIMITATION, *assessment.limitations]))
    if any(rejected.values()):
        invalid_target = CrossSourceTarget(pair=pair, kind="page", key="invalid_page_identity")
        invalid_notes = sorted(set([*notes, "CROSS_SOURCE_PAGE_IDENTITY_REJECTED"]))
        outcomes.extend(outcome(case_id=request.case_id, rule_id=rule, finding_kind=kind,
            target=invalid_target, state="not_checked", reason="page_identity_ambiguous",
            limitations=invalid_notes) for rule, kind in (
                (PAGE_TREND, "business"), (PAGE_OPPORTUNITY, "business"),
                (PAGE_CONFLICT, "measurement"),
            ))
    gsc_total = gsc.current.totals.rows[0] if gsc.current.totals.rows else None
    ga4_total = ga4.current.totals.rows[0] if ga4.current.totals.rows else None
    restricted = {restriction.metric_name for period in (ga4.current, ga4.previous)
        for view in (period.totals, period.landing_pages)
        for restriction in view.metadata.metric_restrictions}
    degraded = any(view.metadata.sampling or view.metadata.subject_to_thresholding or view.metadata.data_loss_from_other_row
        for period in (ga4.current, ga4.previous) for view in (period.totals, period.landing_pages))
    confidence = "low" if degraded else "medium"
    for canonical in keys:
        target = CrossSourceTarget(pair=pair, kind="page", key=bounded_target_key(canonical))
        gc, gp, ac, ap = (groups[(source, period)].get(canonical)
            for source, period in (("gsc", "current"), ("gsc", "previous"), ("ga4", "current"), ("ga4", "previous")))
        detail_truncated = gsc.current.pages.truncated or gsc.previous.pages.truncated \
            or ga4.current.landing_pages.truncated or ga4.previous.landing_pages.truncated
        if not all((gc, gp, ac, ap)):
            reason = "detail_truncated" if detail_truncated else "comparison_unavailable"
            limitations = notes + (["CROSS_SOURCE_PAGE_DETAIL_TRUNCATED"] if detail_truncated else [])
            outcomes.extend(outcome(case_id=request.case_id, rule_id=rule, finding_kind=kind,
                target=target, state="not_checked", reason=reason, limitations=limitations)
                for rule, kind in ((PAGE_TREND, "business"), (PAGE_CONFLICT, "measurement")))
        elif "sessions" in restricted:
            outcomes.extend(outcome(case_id=request.case_id, rule_id=rule, finding_kind=kind,
                target=target, state="not_checked", reason="provider_limitation", limitations=notes)
                for rule, kind in ((PAGE_TREND, "business"), (PAGE_CONFLICT, "measurement")))
        else:
            g_before, a_before = _sum_gsc(gp, "clicks"), _sum_ga4(ap, "sessions")
            g_change = relative_change(_sum_gsc(gc, "clicks"), g_before)
            a_change = relative_change(_sum_ga4(ac, "sessions"), a_before)
            if g_before < 10 or _sum_ga4(ac, "sessions") < 20 or a_before < 20:
                state, reason = "not_checked", "insufficient_sample"
            else:
                aligned = g_change is not None and a_change is not None and abs(g_change) >= .2 and abs(a_change) >= .2 and g_change * a_change > 0
                state, reason = ("triggered", "condition_met") if aligned else ("not_triggered", "condition_not_met")
            evidence = comparator = []
            if state == "triggered":
                evidence = _page_refs(ledger, gsc_snapshot, gc, source="gsc", period="current", metric="clicks", start=gsc.current.start_date, end=gsc.current.end_date) \
                    + _page_refs(ledger, ga4_snapshot, ac, source="ga4", period="current", metric="sessions", start=ga4.current.start_date, end=ga4.current.end_date)
                comparator = _page_refs(ledger, gsc_snapshot, gp, source="gsc", period="previous", metric="clicks", start=gsc.previous.start_date, end=gsc.previous.end_date) \
                    + _page_refs(ledger, ga4_snapshot, ap, source="ga4", period="previous", metric="sessions", start=ga4.previous.start_date, end=ga4.previous.end_date)
            decline = state == "triggered" and (g_change or 0) < 0
            severe = decline and abs(g_change or 0) >= .5 and abs(a_change or 0) >= .5
            outcomes.append(outcome(case_id=request.case_id, rule_id=PAGE_TREND, finding_kind="business",
                target=target, state=state, reason=reason, evidence_ids=evidence, comparator_ids=comparator,
                statement=f"Google organic clicks and GA4 sessions for this page both {'decreased' if decline else 'increased'} by at least 20% versus the preceding 90 days." if state == "triggered" else None,
                severity="high" if severe else "medium", confidence=confidence, impact_score=100 if severe else 50,
                affected_urls=[HttpUrl(canonical)], limitations=notes,
                change_condition="The two page-level indicators no longer move in the same direction by at least 20% with sufficient samples." if state == "triggered" else None))
            conflict = g_change is not None and a_change is not None and abs(g_change) >= .2 and abs(a_change) >= .2 and g_change * a_change < 0 and g_before >= 10 and _sum_ga4(ac, "sessions") >= 20 and a_before >= 20
            evidence = comparator = []
            if conflict:
                evidence = _page_refs(ledger, gsc_snapshot, gc, source="gsc", period="current", metric="clicks", start=gsc.current.start_date, end=gsc.current.end_date) + _page_refs(ledger, ga4_snapshot, ac, source="ga4", period="current", metric="sessions", start=ga4.current.start_date, end=ga4.current.end_date)
                comparator = _page_refs(ledger, gsc_snapshot, gp, source="gsc", period="previous", metric="clicks", start=gsc.previous.start_date, end=gsc.previous.end_date) + _page_refs(ledger, ga4_snapshot, ap, source="ga4", period="previous", metric="sessions", start=ga4.previous.start_date, end=ga4.previous.end_date)
            outcomes.append(outcome(case_id=request.case_id, rule_id=PAGE_CONFLICT, finding_kind="measurement",
                target=target, state="triggered" if conflict else ("not_checked" if reason == "insufficient_sample" else "not_triggered"),
                reason="condition_met" if conflict else reason, evidence_ids=evidence, comparator_ids=comparator,
                statement="Search Console clicks and GA4 sessions moved in opposite directions for the same normalized page." if conflict else None,
                severity="medium", confidence=confidence, impact_score=20, affected_urls=[HttpUrl(canonical)],
                limitations=notes, change_condition="The page-level indicators no longer move in opposite directions by at least 20%." if conflict else None))

        if not gc or not ac:
            outcomes.append(outcome(case_id=request.case_id, rule_id=PAGE_OPPORTUNITY, finding_kind="business",
                target=target, state="not_checked", reason="comparison_unavailable", limitations=notes))
            continue
        g_impressions = _sum_gsc(gc, "impressions")
        g_clicks = _sum_gsc(gc, "clicks")
        g_ctr = g_clicks / g_impressions if g_impressions else 0
        g_position = sum(row.position * row.impressions for row in gc.rows) / g_impressions if g_impressions else 0
        a_sessions = _sum_ga4(ac, "sessions")
        a_engagement = _weighted(ac, "engaged_sessions", "sessions")
        a_events = _sum_ga4(ac, "key_events")
        a_event_rate = a_events / a_sessions if a_sessions else 0
        if restricted.intersection({"sessions", "engagedSessions", "engagementRate", "keyEvents", "sessionKeyEventRate"}):
            opportunity, opportunity_reason = False, "provider_limitation"
        elif not gsc_total or not ga4_total:
            opportunity, opportunity_reason = False, "field_not_observed"
        elif g_impressions < 100 or a_sessions < 20:
            opportunity, opportunity_reason = False, "insufficient_sample"
        else:
            search_gap = 4 <= g_position <= 20 and g_ctr <= gsc_total.ctr * .8
            engagement_gap = ga4_total.engagement_rate > 0 and a_engagement <= ga4_total.engagement_rate * .8
            conversion_gap = bool(ga4.configured_key_events) and (
                a_events == 0 or (ga4_total.session_key_event_rate > 0 and a_event_rate <= ga4_total.session_key_event_rate * .5))
            opportunity = search_gap and (engagement_gap or conversion_gap)
            opportunity_reason = "condition_met" if opportunity else "condition_not_met"
        evidence = comparator = []
        if opportunity:
            evidence = _page_refs(ledger, gsc_snapshot, gc, source="gsc", period="current", metric="impressions", start=gsc.current.start_date, end=gsc.current.end_date) \
                + _page_refs(ledger, gsc_snapshot, gc, source="gsc", period="current", metric="ctr", start=gsc.current.start_date, end=gsc.current.end_date) \
                + _page_refs(ledger, gsc_snapshot, gc, source="gsc", period="current", metric="position", start=gsc.current.start_date, end=gsc.current.end_date) \
                + _page_refs(ledger, ga4_snapshot, ac, source="ga4", period="current", metric="sessions", start=ga4.current.start_date, end=ga4.current.end_date) \
                + _page_refs(ledger, ga4_snapshot, ac, source="ga4", period="current", metric="engagement_rate", start=ga4.current.start_date, end=ga4.current.end_date) \
                + _page_refs(ledger, ga4_snapshot, ac, source="ga4", period="current", metric="key_events", start=ga4.current.start_date, end=ga4.current.end_date)
            comparator = [aggregate_evidence(ledger, gsc_snapshot, gsc_total, source="gsc", target="site_total", period="current", metric="ctr", start=gsc.current.start_date, end=gsc.current.end_date, origin="/current/totals/0/ctr"),
                aggregate_evidence(ledger, ga4_snapshot, ga4_total, source="ga4", target="site_total", period="current", metric="engagement_rate", start=ga4.current.start_date, end=ga4.current.end_date, origin="/current/totals/0/engagement_rate"),
                aggregate_evidence(ledger, ga4_snapshot, ga4_total, source="ga4", target="site_total", period="current", metric="session_key_event_rate", start=ga4.current.start_date, end=ga4.current.end_date, origin="/current/totals/0/session_key_event_rate")]
        severe = opportunity and engagement_gap and conversion_gap
        outcomes.append(outcome(case_id=request.case_id, rule_id=PAGE_OPPORTUNITY, finding_kind="business",
            target=target, state="triggered" if opportunity else ("not_checked" if opportunity_reason not in {"condition_met", "condition_not_met"} else "not_triggered"),
            reason=opportunity_reason, evidence_ids=evidence, comparator_ids=comparator,
            statement="This page has meaningful Google search visibility while GA4 confirms a material engagement or conversion gap." if opportunity else None,
            severity="high" if severe else "medium", confidence=confidence, impact_score=100 if severe else 50,
            affected_urls=[HttpUrl(canonical)], limitations=notes,
            change_condition="The page no longer meets the visibility, ranking, click-through and GA4 quality gates together." if opportunity else None))
    return outcomes


def _gsc_ga4_aggregate(request, parsed, ledger, assessment) -> list[CrossSourceOutcome]:
    pair = "gsc_ga4"
    rules = [(AGGREGATE_TREND, "business", "aggregate", "organic_demand"),
             (AGGREGATE_CONFLICT, "measurement", "aggregate", "organic_demand"),
             (WEEKLY_MOVEMENT, "business", "weekly_series", "organic_weekly"),
             (WEEKLY_CONFLICT, "measurement", "weekly_series", "organic_weekly")]
    if assessment.state != "eligible_for_business":
        return _blocked(request.case_id, pair, assessment, rules)
    gsc: GscSnapshot = parsed["gsc"]
    ga4: Ga4Snapshot = parsed["ga4"]
    gs = next(item for item in request.first_party_input.snapshots if item.source_type == "gsc")
    gas = next(item for item in request.first_party_input.snapshots if item.source_type == "ga4")
    notes = sorted(set([CAUSAL_LIMITATION, *assessment.limitations]))
    gt = [period.totals.rows[0] if period.totals.rows else None for period in (gsc.current, gsc.previous)]
    at = [period.totals.rows[0] if period.totals.rows else None for period in (ga4.current, ga4.previous)]
    outcomes: list[CrossSourceOutcome] = []
    total_restricted = {restriction.metric_name for period in (ga4.current, ga4.previous)
        for restriction in period.totals.metadata.metric_restrictions}
    if "sessions" in total_restricted:
        outcomes.extend(outcome(case_id=request.case_id, rule_id=rule, finding_kind=kind,
            target=CrossSourceTarget(pair=pair, kind="aggregate", key="organic_demand"),
            state="not_checked", reason="provider_limitation", limitations=notes)
            for rule, kind in ((AGGREGATE_TREND, "business"), (AGGREGATE_CONFLICT, "measurement")))
    elif not all(gt + at):
        outcomes.extend(outcome(case_id=request.case_id, rule_id=rule, finding_kind=kind,
            target=CrossSourceTarget(pair=pair, kind="aggregate", key="organic_demand"),
            state="not_checked", reason="comparison_unavailable", limitations=notes)
            for rule, kind in ((AGGREGATE_TREND, "business"), (AGGREGATE_CONFLICT, "measurement")))
    else:
        gc, gp = relative_change(gt[0].clicks, gt[1].clicks), relative_change(at[0].sessions, at[1].sessions)
        sufficient = gt[1].clicks >= 10 and at[0].sessions >= 20 and at[1].sessions >= 20
        aligned = sufficient and gc is not None and gp is not None and abs(gc) >= .2 and abs(gp) >= .2 and gc * gp > 0
        conflict = sufficient and gc is not None and gp is not None and abs(gc) >= .2 and abs(gp) >= .2 and gc * gp < 0
        refs = comps = []
        if aligned or conflict:
            refs = [aggregate_evidence(ledger, gs, gt[0], source="gsc", target="site_total", period="current", metric="clicks", start=gsc.current.start_date, end=gsc.current.end_date, origin="/current/totals/0/clicks"), aggregate_evidence(ledger, gas, at[0], source="ga4", target="site_total", period="current", metric="sessions", start=ga4.current.start_date, end=ga4.current.end_date, origin="/current/totals/0/sessions")]
            comps = [aggregate_evidence(ledger, gs, gt[1], source="gsc", target="site_total", period="previous", metric="clicks", start=gsc.previous.start_date, end=gsc.previous.end_date, origin="/previous/totals/0/clicks"), aggregate_evidence(ledger, gas, at[1], source="ga4", target="site_total", period="previous", metric="sessions", start=ga4.previous.start_date, end=ga4.previous.end_date, origin="/previous/totals/0/sessions")]
        state = "triggered" if aligned else ("not_checked" if not sufficient else "not_triggered")
        direction = "decreased" if (gc or 0) < 0 else "increased"
        severe = aligned and direction == "decreased" and abs(gc or 0) >= .5 and abs(gp or 0) >= .5
        outcomes.append(outcome(case_id=request.case_id, rule_id=AGGREGATE_TREND, finding_kind="business", target=CrossSourceTarget(pair=pair, kind="aggregate", key="organic_demand"), state=state, reason="condition_met" if aligned else ("insufficient_sample" if not sufficient else "condition_not_met"), evidence_ids=refs if aligned else [], comparator_ids=comps if aligned else [], statement=f"Google organic clicks and GA4 sessions both {direction} by at least 20% versus the preceding 90 days." if aligned else None, severity="high" if severe else "medium", confidence="medium", impact_score=100 if severe else 50, limitations=notes, change_condition="The aggregate indicators no longer move in the same direction by at least 20%." if aligned else None))
        outcomes.append(outcome(case_id=request.case_id, rule_id=AGGREGATE_CONFLICT, finding_kind="measurement", target=CrossSourceTarget(pair=pair, kind="aggregate", key="organic_demand"), state="triggered" if conflict else ("not_checked" if not sufficient else "not_triggered"), reason="condition_met" if conflict else ("insufficient_sample" if not sufficient else "condition_not_met"), evidence_ids=refs if conflict else [], comparator_ids=comps if conflict else [], statement="Search Console clicks and GA4 sessions moved in opposite directions at the aggregate level." if conflict else None, severity="medium", confidence="medium", impact_score=20, limitations=notes, change_condition="The aggregate indicators no longer move in opposite directions by at least 20%." if conflict else None))

    date_restricted = {restriction.metric_name for restriction in ga4.current.dates.metadata.metric_restrictions}
    g_dates = {date.fromisoformat(row.key): row.clicks for row in gsc.current.dates.rows if row.key}
    a_dates = {row.date: float(row.sessions) for row in ga4.current.dates.rows}
    start, end = max(gsc.current.start_date, ga4.current.start_date), min(gsc.current.end_date, ga4.current.end_date)
    weeks = complete_week_pairs(g_dates, a_dates, start=start, end=end)
    rho = None if "sessions" in date_restricted else spearman(weeks)
    for rule, kind, triggered, statement in (
        (WEEKLY_MOVEMENT, "business", rho is not None and rho >= .70, "Weekly Search Console clicks and GA4 sessions show strong positive co-movement."),
        (WEEKLY_CONFLICT, "measurement", rho is not None and rho <= -.70, "Weekly Search Console clicks and GA4 sessions show strong inverse movement."),
    ):
        if "sessions" in date_restricted:
            state, reason = "not_checked", "provider_limitation"
        elif rho is None:
            state, reason = "not_checked", "insufficient_sample"
        else:
            state, reason = ("triggered", "condition_met") if triggered else ("not_triggered", "condition_not_met")
        refs = comps = []
        if triggered:
            refs, comps = [], []
            for week_start, g_value, a_value in weeks:
                week_end = week_start.fromordinal(week_start.toordinal() + 6)
                refs.append(ledger.add(source_type="gsc", category="metric", target_key=week_start.isoformat(), field="weekly_clicks", value=g_value, period="current", metric_key="clicks", unit="count", confidence="medium", coverage_start=week_start, coverage_end=week_end, limitations=gs.health_reasons, origin_path=f"/current/dates/week/{week_start.isoformat()}"))
                comps.append(ledger.add(source_type="ga4", category="metric", target_key=week_start.isoformat(), field="weekly_sessions", value=a_value, period="current", metric_key="sessions", unit="count", confidence="medium", coverage_start=week_start, coverage_end=week_end, limitations=gas.health_reasons, origin_path=f"/current/dates/week/{week_start.isoformat()}"))
        outcomes.append(outcome(case_id=request.case_id, rule_id=rule, finding_kind=kind, target=CrossSourceTarget(pair=pair, kind="weekly_series", key="organic_weekly"), state=state, reason=reason, evidence_ids=refs, comparator_ids=comps, statement=statement if triggered else None, severity="medium", confidence="medium", impact_score=50 if kind == "business" else 20, limitations=notes, change_condition="The weekly correlation no longer meets the fixed threshold with at least eight complete weeks." if triggered else None))
    return outcomes


def _gbp_total(performance, metrics, first, last):
    return sum(value for metric in metrics for day, value in performance.get(metric, {}).items() if first <= day <= last)


def _gbp_pairs(request, parsed, ledger, assessments) -> list[CrossSourceOutcome]:
    outcomes: list[CrossSourceOutcome] = []
    if "gbp" not in parsed:
        for pair, business, conflict, key in (("gsc_gbp", GSC_GBP_TREND, GSC_GBP_CONFLICT, "visibility"), ("ga4_gbp", GA4_GBP_TREND, GA4_GBP_CONFLICT, "behavior")):
            assessment = assessments[pair]
            outcomes.extend(_blocked(request.case_id, pair, assessment, [(business, "business", "aggregate", key), (conflict, "measurement", "aggregate", key)]))
        return outcomes
    gbp, performance = parsed["gbp"]
    gbps = next(item for item in request.first_party_input.snapshots if item.source_type == "gbp")
    impression_metrics = first_party_gbp_findings.IMPRESSION_METRICS
    action_metrics = first_party_gbp_findings.ACTION_METRICS
    for pair, business_rule, conflict_rule, other_source, other_metric, gbp_metrics, gbp_key, other_min, gbp_min in (
        ("gsc_gbp", GSC_GBP_TREND, GSC_GBP_CONFLICT, "gsc", "impressions", impression_metrics, "impressions", 50, 50),
        ("ga4_gbp", GA4_GBP_TREND, GA4_GBP_CONFLICT, "ga4", "sessions", action_metrics, "customer_actions", 20, 10),
    ):
        assessment = assessments[pair]
        rules = [(business_rule, "business", "aggregate", gbp_key), (conflict_rule, "measurement", "aggregate", gbp_key)]
        if assessment.state != "eligible_for_business":
            outcomes.extend(_blocked(request.case_id, pair, assessment, rules))
            continue
        other = parsed[other_source]
        others = next(item for item in request.first_party_input.snapshots if item.source_type == other_source)
        current_row = other.current.totals.rows[0] if other.current.totals.rows else None
        previous_row = other.previous.totals.rows[0] if other.previous.totals.rows else None
        notes = sorted(set([CAUSAL_LIMITATION, *assessment.limitations]))
        restricted = {restriction.metric_name for period in (other.current, other.previous)
            for restriction in period.totals.metadata.metric_restrictions} if other_source == "ga4" else set()
        if other_metric in restricted:
            outcomes.extend(outcome(case_id=request.case_id, rule_id=rule, finding_kind=kind,
                target=CrossSourceTarget(pair=pair, kind="aggregate", key=gbp_key), state="not_checked",
                reason="provider_limitation", limitations=notes) for rule, kind, _, _ in rules)
            continue
        if current_row is None or previous_row is None:
            outcomes.extend(outcome(case_id=request.case_id, rule_id=rule, finding_kind=kind,
                target=CrossSourceTarget(pair=pair, kind="aggregate", key=gbp_key), state="not_checked",
                reason="comparison_unavailable", limitations=notes) for rule, kind, _, _ in rules)
            continue
        other_current, other_previous = getattr(current_row, other_metric), getattr(previous_row, other_metric)
        gbp_current = _gbp_total(performance, gbp_metrics, gbp.current.start_date, gbp.current.end_date)
        gbp_previous = _gbp_total(performance, gbp_metrics, gbp.previous.start_date, gbp.previous.end_date)
        sufficient = other_previous >= other_min and gbp_previous >= gbp_min and (other_source != "ga4" or other_current >= other_min)
        other_change = relative_change(other_current, other_previous)
        gbp_change = relative_change(gbp_current, gbp_previous)
        aligned = sufficient and other_change is not None and gbp_change is not None and abs(other_change) >= .2 and abs(gbp_change) >= .2 and other_change * gbp_change > 0
        conflict = sufficient and other_change is not None and gbp_change is not None and abs(other_change) >= .2 and abs(gbp_change) >= .2 and other_change * gbp_change < 0
        exact_refs = exact_comps = []
        if aligned or conflict:
            exact_refs = [aggregate_evidence(ledger, others, current_row, source=other_source, target="site_total", period="current", metric=other_metric, start=other.current.start_date, end=other.current.end_date, origin=f"/current/totals/0/{other_metric}"), categorical_gbp(ledger, gbps, target=gbp_key, field="change_band", value=change_band(gbp_change or 0), period="current_vs_previous", start=gbp.current.start_date, end=gbp.current.end_date, origin="/raw_payload/performance")]
            exact_comps = [aggregate_evidence(ledger, others, previous_row, source=other_source, target="site_total", period="previous", metric=other_metric, start=other.previous.start_date, end=other.previous.end_date, origin=f"/previous/totals/0/{other_metric}"), categorical_gbp(ledger, gbps, target=gbp_key, field="previous_sample_gate", value=f"at_least_{gbp_min}", period="previous", start=gbp.previous.start_date, end=gbp.previous.end_date, origin="/raw_payload/performance")]
        severe = aligned and (other_change or 0) <= -.5 and (gbp_change or 0) <= -.5
        direction = "decreased" if (other_change or 0) < 0 else "increased"
        outcomes.append(outcome(case_id=request.case_id, rule_id=business_rule, finding_kind="business", target=CrossSourceTarget(pair=pair, kind="aggregate", key=gbp_key), state="triggered" if aligned else ("not_checked" if not sufficient else "not_triggered"), reason="condition_met" if aligned else ("insufficient_sample" if not sufficient else "condition_not_met"), evidence_ids=exact_refs if aligned else [], comparator_ids=exact_comps if aligned else [], statement=f"{other_source.upper()} {other_metric} and official GBP {gbp_key} both {direction} by at least 20%." if aligned else None, severity="high" if severe else "medium", confidence="medium", impact_score=100 if severe else 50, limitations=notes, change_condition="The paired indicators no longer move in the same direction by at least 20%." if aligned else None))
        outcomes.append(outcome(case_id=request.case_id, rule_id=conflict_rule, finding_kind="measurement", target=CrossSourceTarget(pair=pair, kind="aggregate", key=gbp_key), state="triggered" if conflict else ("not_checked" if not sufficient else "not_triggered"), reason="condition_met" if conflict else ("insufficient_sample" if not sufficient else "condition_not_met"), evidence_ids=exact_refs if conflict else [], comparator_ids=exact_comps if conflict else [], statement=f"{other_source.upper()} {other_metric} and official GBP {gbp_key} moved in opposite directions." if conflict else None, severity="medium", confidence="medium", impact_score=20, limitations=notes, change_condition="The paired indicators no longer move in opposite directions by at least 20%." if conflict else None))
    return outcomes


def _apply_caps(proposals: list[CrossSourceOutcome], request) -> list[CrossSourceOutcome]:
    retained = {item.finding.finding_id for item in proposals if item.finding is not None}
    def cap(candidates, maximum):
        ordered = sorted(candidates, key=lambda item: (-item.evaluation.impact_score, item.evaluation.target.key.casefold(), item.finding.finding_id))
        for item in ordered[maximum:]:
            retained.discard(item.finding.finding_id)
    page_groups = defaultdict(list)
    aggregate_pairs = defaultdict(list)
    measurements = []
    for item in proposals:
        if item.finding is None:
            continue
        evaluation = item.evaluation
        if evaluation.finding_kind == "measurement": measurements.append(item)
        elif evaluation.target.kind == "page": page_groups[evaluation.rule_id].append(item)
        else: aggregate_pairs[evaluation.target.pair].append(item)
    for values in page_groups.values(): cap(values, request.limits.max_page_business_findings_per_rule)
    for values in aggregate_pairs.values(): cap(values, request.limits.max_aggregate_business_findings_per_pair)
    cap(measurements, request.limits.max_measurement_findings)
    current = [item for item in proposals if item.finding and item.finding.finding_id in retained]
    cap(current, request.limits.max_findings)
    result = []
    for item in proposals:
        if item.finding is None or item.finding.finding_id in retained:
            result.append(item)
        else:
            result.append(CrossSourceOutcome(evaluation=item.evaluation.model_copy(update={
                "state": "not_checked", "reason": "output_limit", "finding_id": None,
                "limitations": sorted(set([*item.evaluation.limitations, "FINDING_OUTPUT_LIMIT"])),
            })))
    return result


def build_cross_source_findings(value: CrossSourceFindingsInput | dict) -> CrossSourceFindingsResult:
    try:
        raw = value.model_dump(mode="json", warnings=False) if isinstance(value, CrossSourceFindingsInput) else value
        request = CrossSourceFindingsInput.model_validate_json(canonical_json_bytes(raw))
    except (ValidationError, TypeError, ValueError):
        raise CrossSourceFindingsError("INPUT_INVALID") from None
    if len(canonical_json_bytes(request)) > request.limits.max_bytes:
        raise CrossSourceFindingsError("LIMIT_EXCEEDED")
    recomputed = build_first_party_findings(request.first_party_input)
    if canonical_json_bytes(recomputed) != canonical_json_bytes(request.first_party_result):
        raise CrossSourceFindingsError("CHECKSUM_MISMATCH")
    parsed = {}
    for snapshot in request.first_party_input.snapshots:
        parsed[snapshot.source_type] = _validate_snapshot(snapshot)
    _validate_domain(request, parsed)
    pair_list = _pair_assessments(request, recomputed, parsed)
    assessments = {item.pair: item for item in pair_list}
    ledger = CrossSourceEvidenceLedger(request.first_party_input.snapshots, maximum=request.limits.max_evidence_items)
    ledger.seed(recomputed.evidence_result)
    proposals = _gsc_ga4_pages(request, parsed, ledger, assessments["gsc_ga4"])
    proposals.extend(_gsc_ga4_aggregate(request, parsed, ledger, assessments["gsc_ga4"]))
    proposals.extend(_gbp_pairs(request, parsed, ledger, assessments))
    if len(proposals) > request.limits.max_evaluations:
        raise CrossSourceFindingsError("LIMIT_EXCEEDED")
    proposals = _apply_caps(proposals, request)
    ordered = sorted(proposals, key=lambda item: (PAIR_ORDER[item.evaluation.target.pair], RULE_ORDER.get(item.evaluation.rule_id, 99), item.evaluation.target.kind, item.evaluation.target.key.casefold()))
    findings = [item.finding for item in proposals if item.finding is not None]
    findings.sort(key=lambda item: (SEVERITY_ORDER[item.severity], RULE_ORDER.get(item.rule_id, 99), item.scope, item.finding_id))
    result = CrossSourceFindingsResult(
        first_party_input_checksum=semantic_first_party_input_checksum(request.first_party_input),
        first_party_result_checksum=request.first_party_result_checksum,
        evidence_result=ledger.result(recomputed.source_assessments), findings=findings,
        rule_evaluations=[item.evaluation for item in ordered], pair_assessments=pair_list,
    )
    if len(canonical_json_bytes(result)) > request.limits.max_bytes:
        raise CrossSourceFindingsError("LIMIT_EXCEEDED")
    return result
