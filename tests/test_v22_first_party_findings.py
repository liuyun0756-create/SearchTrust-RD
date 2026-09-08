from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
from uuid import UUID

import httpx
import pytest

from app.google_connections_v22.ga4 import (
    BASE_LIMITATIONS as GA4_LIMITATIONS,
    DateRow,
    DateView,
    Ga4Period,
    Ga4Snapshot,
    KeyEventConfig,
    KeyEventView,
    LandingPageRow,
    LandingPageView,
    MetricRestriction,
    OverviewMetrics,
    OverviewView,
    ReportMetadata,
    Sampling,
    evaluate_health as evaluate_ga4,
)
from app.google_connections_v22.gbp import GbpProvider, evaluate_health as evaluate_gbp
from app.google_connections_v22.gsc import (
    LIMITATIONS as GSC_LIMITATIONS,
    GscSnapshot,
    MetricRow,
    Period,
    View,
    evaluate_health as evaluate_gsc,
)
from app.jobs_v22.digest import canonical_json_bytes, request_digest
from app.report_v22.first_party_findings import build_first_party_findings
from app.report_v22.first_party_findings_errors import FirstPartyFindingsError
from app.report_v22.first_party_findings_models import (
    FirstPartyFindingsInput,
    FirstPartyFindingsLimits,
    TrustedFirstPartySnapshot,
)


CASE_ID = UUID("30000000-0000-4000-8000-000000000001")
PARENT_ID = UUID("30000000-0000-4000-8000-000000000002")
NOW = datetime(2026, 9, 8, 12, tzinfo=timezone.utc)
END = date(2026, 9, 5)
FIXTURES = Path(__file__).parent / "fixtures/google_connections_v22/gbp"


@pytest.fixture
def anyio_backend():
    return "asyncio"


def view(rows):
    return View(rows=rows, truncated=False, aggregation_type="byProperty")


def gsc_value(*, unhealthy=False):
    current = Period(
        start_date=END - timedelta(days=89), end_date=END,
        totals=view([] if unhealthy else [MetricRow(key=None, clicks=100, impressions=1000, ctr=.1, position=6)]),
        queries=view([] if unhealthy else [
            MetricRow(key="service opportunity", clicks=10, impressions=200, ctr=.05, position=8),
            MetricRow(key="growing query", clicks=30, impressions=180, ctr=.166, position=3),
            MetricRow(key="declining query", clicks=8, impressions=100, ctr=.08, position=6),
        ]),
        pages=view([] if unhealthy else [
            MetricRow(key="https://example.test/service", clicks=5, impressions=150, ctr=.033, position=9),
        ]),
        dates=view([] if unhealthy else [MetricRow(key=END.isoformat(), clicks=2, impressions=20, ctr=.1, position=3)]),
        devices=view([]), countries=view([]),
    )
    previous = Period(
        start_date=END - timedelta(days=179), end_date=END - timedelta(days=90),
        totals=view([MetricRow(key=None, clicks=80, impressions=900, ctr=.089, position=7)]),
        queries=view([
            MetricRow(key="service opportunity", clicks=10, impressions=150, ctr=.067, position=9),
            MetricRow(key="growing query", clicks=20, impressions=150, ctr=.133, position=4),
            MetricRow(key="declining query", clicks=20, impressions=180, ctr=.111, position=5),
        ]),
        pages=view([MetricRow(key="https://example.test/service", clicks=8, impressions=120, ctr=.067, position=10)]),
        dates=view([MetricRow(key=(END - timedelta(days=90)).isoformat(), clicks=2, impressions=20, ctr=.1, position=3)]),
        devices=view([]), countries=view([]),
    )
    return GscSnapshot(resource_id="sc-domain:example.test", current=current, previous=previous,
        limitations=list(GSC_LIMITATIONS))


def metadata():
    return ReportMetadata(time_zone="America/Chicago")


def overview(sessions, engagement, events, rate):
    return OverviewMetrics(sessions=sessions, active_users=max(1, sessions - 5),
        engaged_sessions=int(sessions * engagement), engagement_rate=engagement,
        average_session_duration=40, screen_page_views=sessions + 20,
        key_events=events, session_key_event_rate=rate)


def landing(path, sessions, engagement, events, rate):
    return LandingPageRow(landing_page=path, sessions=sessions,
        engaged_sessions=int(sessions * engagement), engagement_rate=engagement,
        average_session_duration=30, screen_page_views=sessions + 10,
        key_events=events, session_key_event_rate=rate)


def ga4_value(*, no_events=False):
    current_rows = [landing("/weak", 100, .3, 0, 0), landing("/grow", 120, .65, 15, .125)]
    previous_rows = [landing("/weak", 100, .35, 1, .01), landing("/grow", 80, .6, 8, .1)]
    current = Ga4Period(start_date=END - timedelta(days=89), end_date=END,
        totals=OverviewView(rows=[overview(1000, .6, 100, .1)], metadata=metadata()),
        landing_pages=LandingPageView(rows=current_rows, truncated=False, metadata=metadata()),
        dates=DateView(rows=[DateRow(date=END, sessions=20, engaged_sessions=10, key_events=2)], metadata=metadata()),
        key_events=KeyEventView(rows=[] if no_events else [], truncated=False, metadata=metadata()))
    previous = Ga4Period(start_date=END - timedelta(days=179), end_date=END - timedelta(days=90),
        totals=OverviewView(rows=[overview(900, .55, 90, .1)], metadata=metadata()),
        landing_pages=LandingPageView(rows=previous_rows, truncated=False, metadata=metadata()),
        dates=DateView(rows=[DateRow(date=END - timedelta(days=90), sessions=20, engaged_sessions=10, key_events=2)], metadata=metadata()),
        key_events=KeyEventView(rows=[], truncated=False, metadata=metadata()))
    configured = [] if no_events else [KeyEventConfig(event_name="lead", counting_method="ONCE_PER_EVENT",
        custom=True, deletable=True)]
    return Ga4Snapshot(resource_id="properties/12345", host_filter=["example.test"],
        configured_key_events=configured, current=current, previous=previous,
        limitations=list(GA4_LIMITATIONS))


def trusted(source, value, number, *, raw=None):
    health, reasons = {"gsc": evaluate_gsc, "ga4": evaluate_ga4}[source](value) if source != "gbp" else evaluate_gbp(value.snapshot)
    normalized = value.model_dump(mode="json") if source != "gbp" else value.manifest()
    checksum = request_digest(raw if source == "gbp" else normalized)
    return TrustedFirstPartySnapshot(
        snapshot_id=UUID(f"30000000-0000-4000-8000-{number:012d}"), case_id=CASE_ID,
        binding_id=UUID(f"40000000-0000-4000-8000-{number:012d}"), source_type=source,
        schema_version=f"{source}_sync_v1", fetched_at=NOW, expires_at=NOW + timedelta(days=30 if source == "gbp" else 7),
        identity_match_status="matched", health_status=health, health_reasons=reasons,
        normalized_payload=normalized, raw_payload=raw, payload_checksum=checksum,
        external_resource_id=value.snapshot.resource_id if source == "gbp" else value.resource_id,
        coverage_start=value.snapshot.previous.start_date if source == "gbp" else value.previous.start_date,
        coverage_end=value.snapshot.current.end_date if source == "gbp" else value.current.end_date,
    )


def request(*snapshots):
    return FirstPartyFindingsInput(case_id=CASE_ID, parent_report_id=PARENT_ID,
        evaluated_at=NOW + timedelta(hours=1), snapshots=list(snapshots))


def gbp_fixture(name):
    return json.loads((FIXTURES / name).read_text())


async def gbp_collection():
    def handler(req):
        if req.url.host == "mybusinessbusinessinformation.googleapis.com":
            return httpx.Response(200, json=gbp_fixture("location_complete.json"))
        if req.url.path.endswith(":fetchMultiDailyMetricsTimeSeries"):
            return httpx.Response(200, json=gbp_fixture("performance_180_days.json"))
        return httpx.Response(200, json=gbp_fixture("keywords_page_1.json"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        return await GbpProvider(client).collect("locations/12345", "fake", date(2026, 9, 4))


def test_gsc_and_ga4_business_rules_are_single_source_and_deterministic() -> None:
    value = request(trusted("gsc", gsc_value(), 10), trusted("ga4", ga4_value(), 11))
    first = build_first_party_findings(value)
    reordered = value.model_copy(update={"snapshots": list(reversed(value.snapshots))})
    second = build_first_party_findings(reordered)
    assert canonical_json_bytes(first) == canonical_json_bytes(second)
    rules = {finding.rule_id for finding in first.findings}
    assert "V22.FIRST_PARTY.GSC.QUERY_OPPORTUNITY" in rules
    assert "V22.FIRST_PARTY.GSC.PAGE_OPPORTUNITY" in rules
    assert "V22.FIRST_PARTY.GSC.DIMENSION_GROWTH" in rules
    assert "V22.FIRST_PARTY.GSC.DIMENSION_DECLINE" in rules
    assert "V22.FIRST_PARTY.GA4.ENGAGEMENT_GAP" in rules
    assert "V22.FIRST_PARTY.GA4.CONVERSION_GAP" in rules
    assert "V22.FIRST_PARTY.GA4.PAGE_CHANGE" in rules
    evidence = {item.evidence_id: item for item in first.evidence_result.evidence_index}
    for finding in first.findings:
        assert {evidence[key].source_type for key in finding.evidence_ids + finding.comparator_ids} == {
            finding.rule_id.split(".")[2].lower()
        }
    assert first.source_assessments[2].state == "not_checked"
    assert any(item.reason == "source_missing" for item in first.rule_evaluations if item.target.source_type == "gbp")


def test_unhealthy_source_only_produces_measurement_findings() -> None:
    gsc = trusted("gsc", gsc_value(unhealthy=True), 20)
    ga4 = trusted("ga4", ga4_value(no_events=True), 21)
    result = build_first_party_findings(request(gsc, ga4))
    assert result.source_assessments[0].state == "configuration_only"
    assert result.source_assessments[1].state == "configuration_only"
    assert result.findings
    assert all("MEASUREMENT" in finding.rule_id for finding in result.findings)
    assert all(evaluation.state == "not_checked" for evaluation in result.rule_evaluations
        if evaluation.finding_kind == "business")


def test_checksum_and_expiry_never_become_business_findings() -> None:
    gsc = trusted("gsc", gsc_value(), 30)
    ga4 = trusted("ga4", ga4_value(), 31)
    forged = gsc.model_copy(update={"payload_checksum": f"sha256:{'0' * 64}"})
    with pytest.raises(FirstPartyFindingsError, match="V22_FIRST_PARTY_FINDINGS_CHECKSUM_MISMATCH"):
        build_first_party_findings(request(forged, ga4))
    expired = gsc.model_copy(update={"expires_at": NOW + timedelta(minutes=30)})
    result = build_first_party_findings(request(expired, ga4))
    assert result.source_assessments[0].health_status == "expired"
    assert all(item.state == "not_checked" for item in result.rule_evaluations
        if item.target.source_type == "gsc")


@pytest.mark.anyio
async def test_gbp_exact_content_is_not_retained_in_result() -> None:
    collection = await gbp_collection()
    result = build_first_party_findings(request(
        trusted("gsc", gsc_value(), 40), trusted("ga4", ga4_value(), 41),
        trusted("gbp", collection, 42, raw=collection.raw_payload),
    ))
    encoded = canonical_json_bytes(result).decode()
    assert "Example Home Services" not in encoded
    assert "emergency plumber" not in encoded
    gbp_values = [item.normalized_value for item in result.evidence_result.evidence_index if item.source_type == "gbp"]
    assert all(not isinstance(value, (int, float)) for value in gbp_values)
    gbp_scores = [item.impact_score for item in result.rule_evaluations if item.target.source_type == "gbp"]
    assert set(gbp_scores) <= {0, 20, 50, 100}


def test_provider_row_order_does_not_change_semantic_output() -> None:
    gsc = gsc_value()
    ga4 = ga4_value()
    baseline = build_first_party_findings(request(trusted("gsc", gsc, 43), trusted("ga4", ga4, 44)))
    reordered_gsc = gsc.model_copy(update={
        "current": gsc.current.model_copy(update={
            "queries": gsc.current.queries.model_copy(update={"rows": list(reversed(gsc.current.queries.rows))}),
            "pages": gsc.current.pages.model_copy(update={"rows": list(reversed(gsc.current.pages.rows))}),
        }),
        "previous": gsc.previous.model_copy(update={
            "queries": gsc.previous.queries.model_copy(update={"rows": list(reversed(gsc.previous.queries.rows))}),
            "pages": gsc.previous.pages.model_copy(update={"rows": list(reversed(gsc.previous.pages.rows))}),
        }),
    })
    reordered_ga4 = ga4.model_copy(update={
        "current": ga4.current.model_copy(update={
            "landing_pages": ga4.current.landing_pages.model_copy(
                update={"rows": list(reversed(ga4.current.landing_pages.rows))}
            ),
        }),
        "previous": ga4.previous.model_copy(update={
            "landing_pages": ga4.previous.landing_pages.model_copy(
                update={"rows": list(reversed(ga4.previous.landing_pages.rows))}
            ),
        }),
    })
    reordered = build_first_party_findings(request(
        trusted("gsc", reordered_gsc, 43), trusted("ga4", reordered_ga4, 44)
    ))
    assert canonical_json_bytes(reordered) == canonical_json_bytes(baseline)


def test_insufficient_samples_and_missing_comparators_are_not_checked() -> None:
    gsc = gsc_value()
    low = MetricRow(key="small sample", clicks=1, impressions=10, ctr=.1, position=8)
    one_sided = MetricRow(key="one sided", clicks=12, impressions=100, ctr=.12, position=5)
    updated = gsc.model_copy(update={
        "current": gsc.current.model_copy(update={
            "queries": gsc.current.queries.model_copy(update={
                "rows": [*gsc.current.queries.rows, low, one_sided]
            })
        }),
        "previous": gsc.previous.model_copy(update={
            "queries": gsc.previous.queries.model_copy(update={"rows": [*gsc.previous.queries.rows, low]})
        }),
    })
    result = build_first_party_findings(request(
        trusted("gsc", updated, 45), trusted("ga4", ga4_value(), 46)
    ))
    small = [item for item in result.rule_evaluations
        if item.target.source_type == "gsc" and item.target.key == "small sample"]
    assert small
    assert all(item.state == "not_checked" for item in small)
    assert {item.reason for item in small} == {"insufficient_sample"}
    missing = [item for item in result.rule_evaluations
        if item.target.source_type == "gsc" and item.target.key == "one sided"
        and item.rule_id.endswith(("DIMENSION_DECLINE", "DIMENSION_GROWTH"))]
    assert len(missing) == 2
    assert all(item.state == "not_checked" and item.reason == "comparison_unavailable" for item in missing)


def test_output_caps_preserve_evaluations_without_orphan_findings() -> None:
    value = request(trusted("gsc", gsc_value(), 47), trusted("ga4", ga4_value(), 48))
    value = value.model_copy(update={"limits": FirstPartyFindingsLimits(
        max_business_findings_per_source=1,
        max_measurement_findings_per_source=1,
    )})
    result = build_first_party_findings(value)
    assert sum(finding.rule_id.startswith("V22.FIRST_PARTY.GSC") for finding in result.findings) == 1
    assert sum(finding.rule_id.startswith("V22.FIRST_PARTY.GA4") for finding in result.findings) == 1
    assert any(item.reason == "output_limit" for item in result.rule_evaluations)
    finding_ids = {finding.finding_id for finding in result.findings}
    assert {item.finding_id for item in result.rule_evaluations if item.finding_id} == finding_ids


def test_evidence_limit_fails_before_partial_result_is_returned() -> None:
    value = request(trusted("gsc", gsc_value(), 49), trusted("ga4", ga4_value(), 50))
    value = value.model_copy(update={"limits": FirstPartyFindingsLimits(max_evidence_items=1)})
    with pytest.raises(FirstPartyFindingsError, match="V22_FIRST_PARTY_FINDINGS_LIMIT_EXCEEDED"):
        build_first_party_findings(value)


def test_ga4_quality_metadata_caps_confidence_and_only_blocks_affected_metrics() -> None:
    value = ga4_value()
    sampled = ReportMetadata(time_zone="America/Chicago", sampling=[
        Sampling(samples_read_count=50, sampling_space_size=100)
    ])
    sampled_value = value.model_copy(update={
        "current": value.current.model_copy(update={
            "totals": value.current.totals.model_copy(update={"metadata": sampled})
        })
    })
    sampled_result = build_first_party_findings(request(
        trusted("gsc", gsc_value(), 51), trusted("ga4", sampled_value, 52)
    ))
    ga4_business = [finding for finding in sampled_result.findings
        if finding.rule_id.startswith("V22.FIRST_PARTY.GA4") and "MEASUREMENT" not in finding.rule_id]
    assert ga4_business
    assert all(finding.confidence == "low" for finding in ga4_business)

    restricted = ReportMetadata(time_zone="America/Chicago", metric_restrictions=[
        MetricRestriction(metric_name="sessions", restricted_metric_types=["COST_DATA"])
    ])
    restricted_value = value.model_copy(update={
        "current": value.current.model_copy(update={
            "totals": value.current.totals.model_copy(update={"metadata": restricted})
        })
    })
    restricted_result = build_first_party_findings(request(
        trusted("gsc", gsc_value(), 53), trusted("ga4", restricted_value, 54)
    ))
    directly_affected = [item for item in restricted_result.rule_evaluations
        if item.target.source_type == "ga4" and item.rule_id.endswith(("ENGAGEMENT_GAP", "CONVERSION_GAP"))]
    assert directly_affected
    assert all(item.state == "not_checked" and item.reason == "provider_limitation"
        for item in directly_affected)
    assert any(item.rule_id.endswith("PAGE_CHANGE") and item.state == "triggered"
        for item in restricted_result.rule_evaluations)
