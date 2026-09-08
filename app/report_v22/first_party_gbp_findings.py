"""Official GBP-only Findings with durable categorical evidence."""

from __future__ import annotations

from datetime import timedelta

from app.google_connections_v22.gbp import (
    IMPRESSION_METRICS,
    GbpCollection,
    GbpSnapshot,
    KeywordSummary,
    PerformancePeriod,
    normalize_keyword_page,
    normalize_location,
    normalize_performance,
)
from app.report_v22.first_party_findings_common import FirstPartyEvidenceLedger, ProposedOutcome, outcome, relative_change
from app.report_v22.first_party_findings_errors import FirstPartyFindingsError
from app.report_v22.first_party_findings_models import FirstPartyFindingTarget, TrustedFirstPartySnapshot


IMPRESSION_CHANGE = "V22.FIRST_PARTY.GBP.IMPRESSION_CHANGE"
ACTION_CHANGE = "V22.FIRST_PARTY.GBP.CUSTOMER_ACTION_CHANGE"
SEARCH_DEMAND = "V22.FIRST_PARTY.GBP.SEARCH_DEMAND_SIGNAL"
MEASUREMENT = "V22.FIRST_PARTY.GBP.PROFILE_OR_MEASUREMENT"
ACTION_METRICS = frozenset({"CALL_CLICKS", "BUSINESS_DIRECTION_REQUESTS", "WEBSITE_CLICKS"})

MEASUREMENT_REASONS = {
    "GBP_LOCATION_UNVERIFIED": ("The connected Google Business Profile location is not verified.", "high"),
    "GBP_LOCATION_NOT_OPEN": ("The connected Google Business Profile location is not marked open.", "high"),
    "GBP_TITLE_MISSING": ("The connected Google Business Profile is missing its title.", "high"),
    "GBP_WEBSITE_MISSING": ("The connected Google Business Profile is missing its website.", "high"),
    "GBP_PHONE_MISSING": ("The connected Google Business Profile is missing its primary phone.", "medium"),
    "GBP_PRIMARY_CATEGORY_MISSING": ("The connected Google Business Profile is missing its primary category.", "high"),
    "GBP_REGULAR_HOURS_MISSING": ("The connected Google Business Profile is missing regular hours.", "medium"),
    "GBP_ADDRESS_AND_SERVICE_AREA_MISSING": ("The connected Google Business Profile has neither an address nor a service area.", "high"),
    "GBP_NO_CURRENT_IMPRESSIONS": ("No current Google Business Profile impressions were observed in the checked period.", "high"),
}


def _target(kind: str, key: str) -> FirstPartyFindingTarget:
    return FirstPartyFindingTarget(source_type="gbp", kind=kind, key=key)


def decode(snapshot: TrustedFirstPartySnapshot) -> tuple[GbpSnapshot, dict]:
    raw = snapshot.raw_payload
    if raw is None or set(raw) != {"business_information", "performance", "keyword_pages"}:
        raise FirstPartyFindingsError("CONTENT_EXPIRED")
    start = snapshot.coverage_end - timedelta(days=179)
    current_start = snapshot.coverage_end - timedelta(days=89)
    previous_end = snapshot.coverage_end - timedelta(days=90)
    profile = normalize_location(raw["business_information"], snapshot.external_resource_id)
    performance = normalize_performance(raw["performance"], start, snapshot.coverage_end)
    pages = raw["keyword_pages"]
    if not isinstance(pages, list) or not 1 <= len(pages) <= 10:
        raise FirstPartyFindingsError("INPUT_INVALID")
    seen: set[str] = set()
    rows = []
    last_token = ""
    for index, page in enumerate(pages):
        page_rows, next_token = normalize_keyword_page(page, seen)
        if index < len(pages) - 1 and not next_token:
            raise FirstPartyFindingsError("INPUT_INVALID")
        rows.extend(page_rows)
        last_token = next_token
    manifest = snapshot.normalized_payload
    try:
        keywords = KeywordSummary.model_validate(manifest["keywords"])
        limitations = list(manifest["limitations"])
    except (KeyError, TypeError, ValueError):
        raise FirstPartyFindingsError("INPUT_INVALID") from None
    if (
        keywords.pages != len(pages)
        or keywords.available != bool(rows)
        or keywords.threshold_applied != any(row.threshold is not None for row in rows)
        or keywords.truncated != (len(pages) == 10 and bool(last_token))
    ):
        raise FirstPartyFindingsError("CHECKSUM_MISMATCH")

    def impressions(first, last):
        return sum(value for metric in IMPRESSION_METRICS
            for day, value in performance.get(metric, {}).items() if first <= day <= last)

    value = GbpSnapshot(
        resource_id=snapshot.external_resource_id,
        profile_checks=profile,
        current=PerformancePeriod(start_date=current_start, end_date=snapshot.coverage_end,
            impressions=impressions(current_start, snapshot.coverage_end)),
        previous=PerformancePeriod(start_date=start, end_date=previous_end,
            impressions=impressions(start, previous_end)),
        keywords=keywords,
        keyword_rows=rows,
        limitations=limitations,
    )
    if GbpCollection(snapshot=value, raw_payload=raw).manifest() != manifest:
        raise FirstPartyFindingsError("CHECKSUM_MISMATCH")
    return value, performance


def _change_band(change: float) -> str:
    direction = "increase" if change > 0 else "decrease"
    magnitude = "50_plus" if abs(change) >= 0.5 else "20_to_49"
    return f"{direction}_{magnitude}_percent"


def _categorical(ledger, snapshot, *, target, field, value, period, start, end, origin):
    return ledger.add(source_type="gbp", category="metric", target_key=target, field=field,
        value=value, period=period, metric_key=field, unit="category", confidence="high",
        coverage_start=start, coverage_end=end, limitations=snapshot.health_reasons, origin_path=origin)


def evaluate(
    *,
    case_id,
    snapshot: TrustedFirstPartySnapshot,
    value: GbpSnapshot,
    performance: dict,
    ledger: FirstPartyEvidenceLedger,
    business_eligible: bool,
    measurement_eligible: bool,
    blocked_reason: str,
) -> list[ProposedOutcome]:
    outcomes: list[ProposedOutcome] = []
    notes = sorted(set([*value.limitations, *snapshot.health_reasons]))
    for code in sorted(set(snapshot.health_reasons).intersection(MEASUREMENT_REASONS)) if measurement_eligible else []:
        statement, severity = MEASUREMENT_REASONS[code]
        evidence = ledger.add(source_type="gbp", category="coverage", target_key=code,
            field="health_reason", value=code, period="current", confidence="high",
            coverage_start=value.current.start_date, coverage_end=value.current.end_date,
            limitations=notes, origin_path="/health_reasons")
        outcomes.append(outcome(case_id=case_id, rule_id=f"{MEASUREMENT}.{code}", finding_kind="measurement",
            target=_target("coverage", code), state="triggered", reason="condition_met",
            evidence_ids=[evidence], statement=statement, severity=severity, confidence="high",
            impact_score=100 if severity == "high" else 50, limitations=notes,
            change_condition=f"A fresh official GBP snapshot no longer reports {code}."))

    if not business_eligible:
        for rule_id, key in ((IMPRESSION_CHANGE, "impression_change"), (ACTION_CHANGE, "customer_action_change"),
                             (SEARCH_DEMAND, "search_demand")):
            outcomes.append(outcome(case_id=case_id, rule_id=rule_id, finding_kind="business",
                target=_target("aggregate", key), state="not_checked", reason=blocked_reason, limitations=notes))
        return outcomes

    impression_change = relative_change(value.current.impressions, value.previous.impressions)
    if value.previous.impressions < 50:
        state, reason = "not_checked", "insufficient_sample"
    else:
        state = "triggered" if impression_change is not None and abs(impression_change) >= 0.2 else "not_triggered"
        reason = "condition_met" if state == "triggered" else "condition_not_met"
    evidence = comparators = []
    if state == "triggered":
        evidence = [_categorical(ledger, snapshot, target="impressions", field="change_band",
            value=_change_band(impression_change or 0), period="current_vs_previous",
            start=value.current.start_date, end=value.current.end_date,
            origin="/raw_payload/performance")]
        comparators = [_categorical(ledger, snapshot, target="impressions", field="previous_sample_gate",
            value="at_least_50_impressions", period="previous",
            start=value.previous.start_date, end=value.previous.end_date,
            origin="/raw_payload/performance")]
    direction = "increased" if (impression_change or 0) > 0 else "decreased"
    outcomes.append(outcome(case_id=case_id, rule_id=IMPRESSION_CHANGE, finding_kind="business",
        target=_target("aggregate", "impressions"), state=state, reason=reason,
        evidence_ids=evidence, comparator_ids=comparators,
        statement=f"Official Google Business Profile impressions {direction} by at least 20% versus the preceding 90 days."
            if state == "triggered" else None,
        severity="high" if impression_change is not None and impression_change <= -0.5 else "medium",
        confidence="high", impact_score=100 if abs(impression_change or 0) >= 0.5 else 50,
        limitations=notes,
        change_condition="The official GBP impression change is less than 20% in a fresh comparison."
            if state == "triggered" else None))

    def total(metrics, first, last):
        return sum(number for metric in metrics for day, number in performance.get(metric, {}).items()
            if first <= day <= last)

    current_actions = total(ACTION_METRICS, value.current.start_date, value.current.end_date)
    previous_actions = total(ACTION_METRICS, value.previous.start_date, value.previous.end_date)
    action_change = relative_change(current_actions, previous_actions)
    if previous_actions < 10:
        state, reason = "not_checked", "insufficient_sample"
    else:
        state = "triggered" if action_change is not None and abs(action_change) >= 0.2 else "not_triggered"
        reason = "condition_met" if state == "triggered" else "condition_not_met"
    evidence = comparators = []
    if state == "triggered":
        evidence = [_categorical(ledger, snapshot, target="customer_actions", field="change_band",
            value=_change_band(action_change or 0), period="current_vs_previous",
            start=value.current.start_date, end=value.current.end_date, origin="/raw_payload/performance")]
        comparators = [_categorical(ledger, snapshot, target="customer_actions", field="previous_sample_gate",
            value="at_least_10_actions", period="previous",
            start=value.previous.start_date, end=value.previous.end_date, origin="/raw_payload/performance")]
    direction = "increased" if (action_change or 0) > 0 else "decreased"
    outcomes.append(outcome(case_id=case_id, rule_id=ACTION_CHANGE, finding_kind="business",
        target=_target("aggregate", "customer_actions"), state=state, reason=reason,
        evidence_ids=evidence, comparator_ids=comparators,
        statement=f"Official Google Business Profile customer actions {direction} by at least 20% versus the preceding 90 days."
            if state == "triggered" else None,
        severity="high" if action_change is not None and action_change <= -0.5 else "medium",
        confidence="high", impact_score=100 if abs(action_change or 0) >= 0.5 else 50,
        limitations=notes,
        change_condition="The official GBP customer-action change is less than 20% in a fresh comparison."
            if state == "triggered" else None))

    exact_values = [row.value for row in value.keyword_rows if row.value is not None and row.value > 0]
    if not exact_values:
        outcomes.append(outcome(case_id=case_id, rule_id=SEARCH_DEMAND, finding_kind="business",
            target=_target("search_demand", "leading_term"), state="not_checked",
            reason="provider_limitation", limitations=notes))
    else:
        maximum = max(exact_values)
        tier = "high" if maximum >= 100 else "medium" if maximum >= 20 else "observed"
        evidence = _categorical(ledger, snapshot, target="leading_search_term", field="demand_tier",
            value=tier, period="keyword_months", start=value.current.start_date, end=value.current.end_date,
            origin="/raw_payload/keyword_pages")
        outcomes.append(outcome(case_id=case_id, rule_id=SEARCH_DEMAND, finding_kind="business",
            target=_target("search_demand", "leading_term"), state="triggered", reason="condition_met",
            evidence_ids=[evidence],
            statement=f"Official GBP data contains a leading search-demand theme in the {tier} observed demand band.",
            severity="medium", confidence="medium",
            impact_score={"high": 100, "medium": 50, "observed": 20}[tier], limitations=notes,
            change_condition="A fresh official GBP snapshot no longer contains an exact search-demand value in this band."))
    return outcomes
