from copy import deepcopy
from datetime import timedelta

import pytest

from app.jobs_v22.digest import canonical_json_bytes, request_digest
from app.report_v22.cross_source_findings import (
    GA4_GBP_TREND, GSC_GBP_TREND, PAGE_CONFLICT, PAGE_OPPORTUNITY, PAGE_TREND,
    WEEKLY_MOVEMENT, build_cross_source_findings,
)
from app.report_v22.cross_source_findings_errors import CrossSourceFindingsError
from app.report_v22.cross_source_findings_models import CrossSourceFindingsInput
from app.report_v22.first_party_findings import build_first_party_findings
from test_v22_first_party_findings import (
    CASE_ID, NOW, PARENT_ID, gbp_collection, ga4_value, gsc_value, landing, request, trusted,
)
from app.google_connections_v22.gsc import MetricRow
from app.google_connections_v22.ga4 import DateRow
from app.google_connections_v22.gbp import GbpCollection, PerformancePeriod


@pytest.fixture
def anyio_backend():
    return "asyncio"


def cross_request(gsc=None, ga4=None, gbp=None):
    gsc = gsc or gsc_value()
    ga4 = ga4 or ga4_value()
    snapshots = [trusted("gsc", gsc, 901), trusted("ga4", ga4, 902)]
    if gbp is not None:
        snapshots.append(trusted("gbp", gbp, 903, raw=gbp.raw_payload))
    source = request(*snapshots)
    first_party = build_first_party_findings(source)
    return CrossSourceFindingsInput(
        case_id=CASE_ID, parent_report_id=PARENT_ID, evaluated_at=NOW + timedelta(hours=1),
        normalized_domain="example.test", first_party_input=source, first_party_result=first_party,
        first_party_input_checksum=request_digest(source), first_party_result_checksum=request_digest(first_party),
    )


def aligned_values(*, conflict=False):
    gsc = gsc_value()
    current_page = MetricRow(key="http://www.example.test/service/?utm_source=x", clicks=30,
        impressions=750, ctr=.04, position=8)
    previous_page = MetricRow(key="https://example.test/service", clicks=20,
        impressions=200, ctr=.1, position=9)
    gsc = gsc.model_copy(update={
        "current": gsc.current.model_copy(update={"pages": gsc.current.pages.model_copy(update={"rows": [current_page]})}),
        "previous": gsc.previous.model_copy(update={"pages": gsc.previous.pages.model_copy(update={"rows": [previous_page]})}),
    })
    ga4 = ga4_value()
    current_sessions, previous_sessions = (20, 50) if conflict else (60, 30)
    current_page_ga = landing("/service?gclid=x", current_sessions, .25, 0, 0)
    previous_page_ga = landing("/service/", previous_sessions, .5, 2, .04)
    ga4 = ga4.model_copy(update={
        "current": ga4.current.model_copy(update={"landing_pages": ga4.current.landing_pages.model_copy(update={"rows": [current_page_ga]})}),
        "previous": ga4.previous.model_copy(update={"landing_pages": ga4.previous.landing_pages.model_copy(update={"rows": [previous_page_ga]})}),
    })
    return gsc, ga4


def test_page_alignment_and_confirmed_opportunity_reference_exact_pair() -> None:
    gsc, ga4 = aligned_values()
    result = build_cross_source_findings(cross_request(gsc, ga4))
    rules = {finding.rule_id for finding in result.findings}
    assert PAGE_TREND in rules
    assert PAGE_OPPORTUNITY in rules
    evidence = {item.evidence_id: item for item in result.evidence_result.evidence_index}
    for finding in result.findings:
        evaluation = next(item for item in result.rule_evaluations if item.finding_id == finding.finding_id)
        assert {evidence[key].source_type for key in finding.evidence_ids + finding.comparator_ids} == \
            set(evaluation.target.pair.split("_"))
        assert "CROSS_SOURCE_COVARIATION_DOES_NOT_ESTABLISH_CAUSATION" in finding.missing_data
    assert "THIRD_SOURCE_GBP_MISSING" in next(item for item in result.pair_assessments if item.pair == "gsc_ga4").limitations


def test_page_opposite_direction_is_measurement_not_business() -> None:
    gsc, ga4 = aligned_values(conflict=True)
    result = build_cross_source_findings(cross_request(gsc, ga4))
    conflict = next(item for item in result.rule_evaluations if item.rule_id == PAGE_CONFLICT and item.target.key.endswith("/service"))
    trend = next(item for item in result.rule_evaluations if item.rule_id == PAGE_TREND and item.target.key.endswith("/service"))
    assert conflict.state == "triggered"
    assert conflict.impact_score == 20
    assert trend.state == "not_triggered"


def test_recomputes_v22_070_instead_of_trusting_resigned_result() -> None:
    value = cross_request()
    findings = list(value.first_party_result.findings)
    findings[0] = findings[0].model_copy(update={"statement": "Modified but structurally valid statement."})
    changed = value.first_party_result.model_copy(update={"findings": findings})
    resigned = value.model_copy(update={
        "first_party_result": changed,
        "first_party_result_checksum": request_digest(changed),
    })
    with pytest.raises(CrossSourceFindingsError, match="V22_CROSS_SOURCE_FINDINGS_CHECKSUM_MISMATCH"):
        build_cross_source_findings(resigned)


def test_snapshot_order_does_not_change_output() -> None:
    gsc, ga4 = aligned_values()
    value = cross_request(gsc, ga4)
    reversed_input = value.first_party_input.model_copy(update={"snapshots": list(reversed(value.first_party_input.snapshots))})
    reversed_first_party = build_first_party_findings(reversed_input)
    reordered = value.model_copy(update={
        "first_party_input": reversed_input, "first_party_result": reversed_first_party,
        "first_party_input_checksum": request_digest(reversed_input),
        "first_party_result_checksum": request_digest(reversed_first_party),
    })
    assert canonical_json_bytes(build_cross_source_findings(value)) == canonical_json_bytes(build_cross_source_findings(reordered))


@pytest.mark.anyio
async def test_official_gbp_exact_values_and_keyword_text_are_never_persisted() -> None:
    gbp = await gbp_collection()
    result = build_cross_source_findings(cross_request(gbp=gbp))
    encoded = canonical_json_bytes(result).decode()
    assert "Example Home Services" not in encoded
    assert "emergency plumber" not in encoded
    values = [item.normalized_value for item in result.evidence_result.evidence_index if item.source_type == "gbp"]
    assert all(not isinstance(value, (int, float)) for value in values)
    scores = [item.impact_score for item in result.rule_evaluations if "gbp" in item.target.pair]
    assert set(scores) <= {0, 20, 50, 100}


def _weekly_values():
    gsc = gsc_value()
    ga4 = ga4_value()
    g_rows, a_rows = [], []
    for index in range(90):
        day = gsc.current.start_date + timedelta(days=index)
        value = index % 17 + 1
        g_rows.append(MetricRow(key=day.isoformat(), clicks=float(value), impressions=float(value * 10), ctr=.1, position=5))
        a_rows.append(DateRow(date=day, sessions=value * 3, engaged_sessions=value * 2, key_events=float(value)))
    gsc = gsc.model_copy(update={"current": gsc.current.model_copy(update={"dates": gsc.current.dates.model_copy(update={"rows": g_rows})})})
    ga4 = ga4.model_copy(update={"current": ga4.current.model_copy(update={"dates": ga4.current.dates.model_copy(update={"rows": a_rows})})})
    return gsc, ga4


def test_weekly_rule_requires_and_uses_complete_aligned_weeks() -> None:
    gsc, ga4 = _weekly_values()
    result = build_cross_source_findings(cross_request(gsc, ga4))
    evaluation = next(item for item in result.rule_evaluations if item.rule_id == WEEKLY_MOVEMENT)
    assert evaluation.state == "triggered"
    evidence = {item.evidence_id: item for item in result.evidence_result.evidence_index}
    assert {evidence[key].source_type for key in evaluation.evidence_ids + evaluation.comparator_ids} == {"gsc", "ga4"}


def _aligned_gbp(collection):
    raw = deepcopy(collection.raw_payload)
    series = raw["performance"]["multiDailyMetricTimeSeries"][0]["dailyMetricTimeSeries"]
    impression_metrics = {item["dailyMetric"] for item in series[:4]}
    for item in series:
        previous = 25 if item["dailyMetric"] in impression_metrics else 5
        current = 50 if item["dailyMetric"] in impression_metrics else 10
        item["timeSeries"]["datedValues"] = [
            {"date": {"year": 2026, "month": 4, "day": 1}, "value": str(previous)},
            {"date": {"year": 2026, "month": 8, "day": 1}, "value": str(current)},
        ]
    snapshot = collection.snapshot.model_copy(update={
        "current": PerformancePeriod(start_date=collection.snapshot.current.start_date,
            end_date=collection.snapshot.current.end_date, impressions=200),
        "previous": PerformancePeriod(start_date=collection.snapshot.previous.start_date,
            end_date=collection.snapshot.previous.end_date, impressions=100),
    })
    return GbpCollection(snapshot=snapshot, raw_payload=raw)


@pytest.mark.anyio
async def test_official_gbp_pair_rules_use_only_categorical_gbp_evidence() -> None:
    gbp = _aligned_gbp(await gbp_collection())
    gsc = gsc_value()
    gsc_current = gsc.current.totals.rows[0].model_copy(update={"impressions": 1500.0})
    gsc_previous = gsc.previous.totals.rows[0].model_copy(update={"impressions": 1000.0})
    gsc = gsc.model_copy(update={
        "current": gsc.current.model_copy(update={"totals": gsc.current.totals.model_copy(update={"rows": [gsc_current]})}),
        "previous": gsc.previous.model_copy(update={"totals": gsc.previous.totals.model_copy(update={"rows": [gsc_previous]})}),
    })
    ga4 = ga4_value()
    ga_current = ga4.current.totals.rows[0].model_copy(update={"sessions": 1200})
    ga_previous = ga4.previous.totals.rows[0].model_copy(update={"sessions": 800})
    ga4 = ga4.model_copy(update={
        "current": ga4.current.model_copy(update={"totals": ga4.current.totals.model_copy(update={"rows": [ga_current]})}),
        "previous": ga4.previous.model_copy(update={"totals": ga4.previous.totals.model_copy(update={"rows": [ga_previous]})}),
    })
    result = build_cross_source_findings(cross_request(gsc, ga4, gbp))
    assert next(item for item in result.rule_evaluations if item.rule_id == GSC_GBP_TREND).state == "triggered"
    assert next(item for item in result.rule_evaluations if item.rule_id == GA4_GBP_TREND).state == "triggered"
    gbp_values = [item.normalized_value for item in result.evidence_result.evidence_index if item.source_type == "gbp"]
    assert gbp_values and all(isinstance(value, str) for value in gbp_values)


def test_page_business_output_is_capped_per_rule_family() -> None:
    gsc = gsc_value()
    ga4 = ga4_value()
    g_current, g_previous, a_current, a_previous = [], [], [], []
    for index in range(7):
        path = f"/service-{index}"
        g_current.append(MetricRow(key=f"https://example.test{path}", clicks=30, impressions=750, ctr=.04, position=8))
        g_previous.append(MetricRow(key=f"https://example.test{path}", clicks=20, impressions=500, ctr=.04, position=9))
        a_current.append(landing(path, 60, .25, 0, 0))
        a_previous.append(landing(path, 30, .5, 2, .04))
    gsc = gsc.model_copy(update={
        "current": gsc.current.model_copy(update={"pages": gsc.current.pages.model_copy(update={"rows": g_current})}),
        "previous": gsc.previous.model_copy(update={"pages": gsc.previous.pages.model_copy(update={"rows": g_previous})}),
    })
    ga4 = ga4.model_copy(update={
        "current": ga4.current.model_copy(update={"landing_pages": ga4.current.landing_pages.model_copy(update={"rows": a_current})}),
        "previous": ga4.previous.model_copy(update={"landing_pages": ga4.previous.landing_pages.model_copy(update={"rows": a_previous})}),
    })
    result = build_cross_source_findings(cross_request(gsc, ga4))
    assert sum(finding.rule_id == PAGE_TREND for finding in result.findings) == 5
    assert sum(finding.rule_id == PAGE_OPPORTUNITY for finding in result.findings) == 5
    capped = [item for item in result.rule_evaluations if item.reason == "output_limit"]
    assert len(capped) == 4
