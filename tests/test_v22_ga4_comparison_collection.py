"""Mock-transport/real-ledger acceptance; no external network or credits."""
import asyncio
from copy import deepcopy
from datetime import date, datetime, timezone
import json
from uuid import uuid4

import fakeredis.aioredis
import httpx
import pytest

from app.google_connections_v22.ga4 import SyncError
from app.google_connections_v22.ga4_comparison import Ga4ComparisonProvider
from app.jobs_v22.cost_ledger import JobCostLedger, CostLimitExceeded
from app.jobs_v22.cost_models import PricingCatalog
from app.jobs_v22.digest import verified_request_digest
from app.report_v22.ga4_comparison_collection import prepare_ga4_comparison, collect_ga4_comparison
from app.report_v22.ga4_comparison_models import (
    Ga4ComparisonCollection, Ga4ExactSnapshot, qualify_ga4,
)
from tests.support.ga4_comparison_fixtures import AT, google, record, options, scope, fixture


@pytest.fixture
def anyio_backend(): return "asyncio"


@pytest.fixture
async def ledger():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    ledger = JobCostLedger(redis, prefix="test:ga4-exact", job_id=uuid4(), ttl_seconds=604800,
        pricing=PricingCatalog(), job_created_at=datetime.now(timezone.utc))
    try: yield ledger
    finally: await redis.aclose()


async def collect(ledger, handler=google, saved=None, **changes):
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = Ga4ComparisonProvider(client, clock=lambda: datetime.fromisoformat(AT.replace("Z", "+00:00")))
        return await collect_ga4_comparison(saved or record(), **options(**changes), provider=provider, token="private-token", cost_ledger=ledger)


@pytest.mark.anyio
@pytest.mark.parametrize("days", [7, 14, 28, 90])
@pytest.mark.parametrize("path", [None, "/service"])
async def test_exact_organic_scope_and_original_aggregates(ledger, days, path):
    calls = []
    def response(req): calls.append(req); return google(req)
    value = await collect(ledger, response, days=days, scope=scope(landing_path=path))
    assert value.state == "collected" and len(calls) == 8
    assert [r.method for r in calls] == ["GET", *["POST"]*6, "GET"]
    assert "private-token" not in value.model_dump_json()
    assert value.snapshot.previous.totals.rows[0].sessions == 10  # Not the sum of daily estimates.
    assert value.snapshot.windows.before.end == date(2026, 8, 31)
    assert value.snapshot.windows.after.start == date(2026, 9, 2)
    for i, request in enumerate(calls[1:-1]):
        assert request.url.host == "analyticsdata.googleapis.com"
        body = json.loads(request.content)
        window = value.plan.windows.before if i < 3 else value.plan.windows.after
        assert body["dateRanges"] == [dict(startDate=window.start.isoformat(), endDate=window.end.isoformat())]
        filters = body["dimensionFilter"]["andGroup"]["expressions"]
        assert filters[0]["filter"]["stringFilter"]["value"] == "web"
        assert filters[1]["filter"]["inListFilter"]["values"] == ["example.test"]
        assert filters[2]["filter"] == {"fieldName": "sessionDefaultChannelGroup", "stringFilter": {"matchType": "EXACT", "value": "Organic Search", "caseSensitive": True}}
        assert len(filters) == (4 if path else 3)
        if path: assert filters[3]["filter"]["stringFilter"]["value"] == path
        assert body["keepEmptyRows"] is False and body["offset"] == "0"
        assert body["limit"] == ["1", "90", "101"][i % 3]
    q = value.qualification
    assert q.complete_daily_observations and q.processing_completeness == "not_attested"
    assert q.event_history == "unverified" and q.event_configuration == "same_at_collection"
    assert q.effect_conclusion is None and not q.causal_attribution
    assert all(m.state == "not_comparable" for m in q.metrics)
    assert Ga4ComparisonCollection.model_validate_json(value.model_dump_json()) == value
    assert (await ledger.snapshot()).root["ga4_attempts"] == 8


@pytest.mark.anyio
async def test_waiting_is_zero_io_and_calendar_elapsed_is_not_finality(ledger):
    def forbidden(_): raise AssertionError("no request while waiting")
    result = await collect(None, forbidden, days=90, at="2026-12-01T07:59:59Z")
    assert result.state == "waiting" and result.snapshot is None and result.qualification is None
    assert result.plan.expected_report_requests == result.plan.max_configuration_requests == 0
    result = await collect(ledger, days=90, at="2026-12-01T08:00:00Z")
    assert result.qualification.processing_completeness == "not_attested"


@pytest.mark.parametrize("changes", [{"days": True}, {"days": 30}, {"action_id": "missing"}, {"at": "2026-01-01T00:00:00Z"},
    {"scope": scope(resource_id="https://private.invalid")}, {"scope": scope(channel="all")},
    {"scope": scope(hosts=["Example.test"])}, {"scope": scope(hosts=["example.test", "example.test"])},
    {"scope": scope(hosts=["localhost"])}, {"scope": scope(timezone="bad/zone")},
    *[{"scope": scope(landing_path=p)} for p in ("https://example.test/service", "//example.test/a", "/a?email=x", "/a#part", "/a\n", "/a\\b")]])
def test_invalid_plans_cannot_reach_a_provider(changes):
    with pytest.raises((ValueError, TypeError, KeyError)):
        prepare_ga4_comparison(record(), **options(**changes))


def test_calendar_uses_source_timezone_and_saved_record_checksum():
    value = prepare_ga4_comparison(record(timezone="Asia/Shanghai"), **options())
    assert value.windows.excluded.start.isoformat() == "2026-08-31"
    assert value.windows.excluded.end.isoformat() == "2026-09-01"
    with pytest.raises(ValueError): prepare_ga4_comparison(record().model_copy(update={"revision": 999}), **options())


@pytest.mark.anyio
async def test_shared_cost_budget_does_not_reset(ledger):
    calls = []
    def response(req): calls.append(req); return google(req)
    await collect(ledger, response)
    with pytest.raises(CostLimitExceeded): await collect(ledger, response)
    assert len(calls) == 11  # 8 first, then config + 2 remaining report claims.
    assert (await ledger.snapshot()).root["ga4_attempts"] == 11


@pytest.mark.anyio
async def test_requires_ledger_before_requests():
    with pytest.raises(SyncError, match="SYNC_COST_LEDGER_REQUIRED"): await collect(None)


@pytest.mark.anyio
@pytest.mark.parametrize("status,code", [(401, "SYNC_GOOGLE_TOKEN_EXPIRED"), (403, "SYNC_GOOGLE_ACCESS_DENIED"),
    (429, "SYNC_GOOGLE_UNAVAILABLE"), (500, "SYNC_GOOGLE_UNAVAILABLE"), (302, "SYNC_GOOGLE_REJECTED")])
async def test_provider_errors_do_not_return_partial_collection(ledger, status, code):
    calls = []
    def response(req):
        calls.append(req)
        return httpx.Response(status, json={"private": "secret"}) if len(calls) == 4 else google(req)
    with pytest.raises(SyncError, match=code): await collect(ledger, response)
    assert len(calls) == 4 and (await ledger.snapshot()).root["ga4_failures"] == 1


@pytest.mark.anyio
@pytest.mark.parametrize("kind", ["timezone", "duplicate", "outside", "rowcount", "flag", "rate", "schema"])
async def test_malformed_reports_fail_closed(ledger, kind):
    def response(req):
        payload = google(req).json()
        if req.method == "POST":
            if kind == "timezone": payload["metadata"]["timeZone"] = "UTC"
            if kind == "rowcount": payload["rowCount"] += 1
            if kind == "flag": payload["metadata"]["subjectToThresholding"] = "false"
            if kind == "schema": payload["metricHeaders"][0]["name"] = "untrusted"
            if kind == "rate" and not json.loads(req.content)["dimensions"]: payload["rows"][0]["metricValues"][3]["value"] = "2"
            if json.loads(req.content)["dimensions"] == [{"name": "date"}]:
                if kind == "duplicate": payload["rows"][1] = deepcopy(payload["rows"][0])
                if kind == "outside": payload["rows"][0]["dimensionValues"][0]["value"] = "20000101"
        return httpx.Response(200, json=payload)
    with pytest.raises(SyncError): await collect(ledger, response)


@pytest.mark.anyio
async def test_configuration_pagination_uses_same_twenty_request_budget(ledger):
    calls = []
    def response(req):
        calls.append(req)
        return httpx.Response(200, json={"keyEvents": [], "nextPageToken": f"page{len(calls)}"})
    with pytest.raises(SyncError, match="SYNC_INVALID_GOOGLE_RESPONSE"): await collect(ledger, response)
    assert len(calls) == 20 and (await ledger.snapshot()).root["ga4_attempts"] == 20


@pytest.mark.anyio
async def test_changed_configuration_and_truncated_events_are_collected_but_not_qualified(ledger):
    observations = []
    def response(req):
        payload = google(req).json()
        if req.method == "GET":
            observations.append(req)
            if len(observations) == 2: payload["keyEvents"][0]["countingMethod"] = "ONCE_PER_SESSION"
        elif json.loads(req.content)["dimensions"] == [{"name": "eventName"}]:
            payload["rowCount"] = 8  # Partial page, even below the 100-row cap.
        return httpx.Response(200, json=payload)
    result = await collect(ledger, response)
    assert result.qualification.event_configuration == "changed_during_collection"
    assert result.snapshot.current.key_events.truncated
    assert "GA4_KEY_EVENT_TRUNCATED" in result.qualification.metrics[3].reasons
    assert "GA4_EVENT_CONFIGURATION_CHANGED" in result.qualification.metrics[3].reasons
    assert "GA4_EVENT_CONFIGURATION_CHANGED" not in result.qualification.metrics[0].reasons


@pytest.mark.anyio
@pytest.mark.parametrize("kind", ["network", "oversized", "invalid_json", "config_loop"])
async def test_transport_bounds_and_config_loop_do_not_retry(ledger, kind):
    calls = []
    def response(req):
        calls.append(req)
        if kind == "network": raise httpx.ConnectError("private failure")
        if kind == "oversized": return httpx.Response(200, content="x"*2_000_001)
        if kind == "invalid_json": return httpx.Response(200, content="not json")
        return httpx.Response(200, json={"keyEvents": [], "nextPageToken": "same"})
    with pytest.raises(SyncError) as error: await collect(ledger, response)
    assert "private" not in str(error.value)
    assert len(calls) == (2 if kind == "config_loop" else 1)


def mutated_snapshot(change):
    raw = fixture().snapshot.model_dump(mode="json")
    change(raw)
    return Ga4ExactSnapshot.model_validate_json(json.dumps(raw))


@pytest.mark.parametrize("kind,expected", [("gap", "GA4_DAILY_OBSERVATIONS_INCOMPLETE"), ("empty", "GA4_AGGREGATE_NOT_OBSERVED"),
    ("sampling", "GA4_REPORT_RESTRICTED"), ("threshold", "GA4_REPORT_RESTRICTED"), ("other", "GA4_REPORT_RESTRICTED"),
    ("empty_reason", "GA4_PROVIDER_EMPTY_REASON")])
def test_coverage_and_restrictions_are_not_silently_qualified(kind, expected):
    def change(raw):
        p = raw["current"]
        if kind == "gap": p["dates"]["rows"].pop()
        if kind == "empty": p["totals"]["rows"] = []
        if kind == "sampling": p["totals"]["metadata"]["sampling"] = [dict(samples_read_count=10, sampling_space_size=20)]
        if kind == "threshold": p["totals"]["metadata"]["subject_to_thresholding"] = True
        if kind == "other": p["totals"]["metadata"]["data_loss_from_other_row"] = True
        if kind == "empty_reason": p["totals"]["metadata"]["empty_reason"] = "DATA_UNAVAILABLE"
    result = qualify_ga4(mutated_snapshot(change))
    assert all(expected in m.reasons for m in result.metrics)
    assert result.processing_completeness == "not_attested"


@pytest.mark.parametrize("kind,reason", [("absent", "GA4_NO_CONFIGURED_KEY_EVENTS"), ("changed", "GA4_EVENT_CONFIGURATION_CHANGED"),
    ("unknown_method", "GA4_EVENT_COUNTING_METHOD_UNKNOWN"), ("unknown_time", "GA4_EVENT_CREATION_TIME_UNKNOWN"),
    ("recent", "GA4_EVENT_NOT_PRESENT_FOR_FULL_BASELINE"), ("truncated", "GA4_KEY_EVENT_TRUNCATED"),
    ("no_activity", "GA4_EVENT_ACTIVITY_NOT_OBSERVED")])
def test_event_qualification_does_not_block_traffic_metrics(kind, reason):
    def change(raw):
        for side in ("configuration_before", "configuration_after"):
            if kind == "absent": raw[side]["events"] = []
            if kind == "unknown_method": raw[side]["events"][0]["counting_method"] = "COUNTING_METHOD_UNSPECIFIED"
            if kind == "unknown_time": raw[side]["events"][0]["create_time"] = None
            if kind == "recent": raw[side]["events"][0]["create_time"] = "2026-09-20T00:00:00Z"
        if kind == "changed": raw["configuration_after"]["events"][0]["counting_method"] = "ONCE_PER_SESSION"
        if kind == "truncated": raw["current"]["key_events"]["truncated"] = True
        if kind == "no_activity": raw["current"]["key_events"]["rows"] = []
    result = qualify_ga4(mutated_snapshot(change))
    assert all(reason in m.reasons and "GA4_EVENT_HISTORY_NOT_VERIFIED" in m.reasons for m in result.metrics[3:])
    assert all(reason not in m.reasons and "GA4_EVENT_HISTORY_NOT_VERIFIED" not in m.reasons for m in result.metrics[:3])


def test_zero_session_rate_denominator_and_metric_specific_restrictions():
    def change(raw):
        raw["current"]["totals"]["rows"][0]["sessions"] = 0
        raw["current"]["totals"]["metadata"]["metric_restrictions"] = [dict(metric_name="keyEvents", restricted_metric_types=["REVENUE_DATA"])]
    metrics = {m.metric: m for m in qualify_ga4(mutated_snapshot(change)).metrics}
    assert "GA4_ZERO_SESSION_DENOMINATOR" in metrics["engagement_rate"].reasons
    assert "GA4_ZERO_SESSION_DENOMINATOR" not in metrics["sessions"].reasons
    assert "GA4_METRIC_RESTRICTED" in metrics["key_events"].reasons
    assert "GA4_METRIC_RESTRICTED" not in metrics["sessions"].reasons


def test_historical_event_absent_from_current_settings_and_mutated_models():
    result = qualify_ga4(mutated_snapshot(lambda raw: raw["current"]["key_events"]["rows"][0].update(event_name="old_event")))
    assert "GA4_REPORTED_EVENT_NOT_IN_CURRENT_CONFIGURATION" in result.metrics[3].reasons
    snap = fixture().snapshot
    snap.current.dates.rows.append(deepcopy(snap.current.dates.rows[0]))
    with pytest.raises(ValueError): qualify_ga4(snap)


@pytest.mark.anyio
async def test_direct_provider_revalidates_waiting_and_clock_before_io(ledger):
    def forbidden(_): raise AssertionError("invalid plan must not make requests")
    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
        provider = Ga4ComparisonProvider(client, clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc))
        waiting = prepare_ga4_comparison(record(), **options(days=90, at="2026-12-01T07:59:59Z"))
        with pytest.raises(SyncError, match="SYNC_WINDOW_NOT_ELAPSED"):
            await provider.collect_exact(waiting, "token", cost_ledger=ledger)
        with pytest.raises(SyncError, match="SYNC_INVALID_CLOCK"):
            await provider.collect_exact(prepare_ga4_comparison(record(), **options()), "token", cost_ledger=ledger)


@pytest.mark.parametrize("kind", ["scope", "window", "qualification", "future", "duplicate_config", "order"])
def test_resigned_collection_cannot_hide_binding_or_qualification_failures(kind):
    raw = fixture().model_dump(mode="json")
    if kind == "scope": raw["snapshot"]["scope"]["resource_id"] = "properties/999"
    if kind == "window": raw["snapshot"]["current"]["start_date"] = "2026-09-03"
    if kind == "qualification": raw["qualification"]["metrics"][0]["reasons"] = ["MADE_UP"]
    if kind == "future": raw["snapshot"]["configuration_before"]["events"][0]["create_time"] = "2099-01-01T00:00:00Z"
    if kind == "duplicate_config": raw["snapshot"]["configuration_before"]["events"] *= 2
    if kind == "order": raw["snapshot"]["completed_at"] = "2026-01-01T00:00:00Z"
    raw["checksum"] = verified_request_digest({k: v for k, v in raw.items() if k != "checksum"})
    with pytest.raises(ValueError): Ga4ComparisonCollection.model_validate_json(json.dumps(raw))


@pytest.mark.anyio
async def test_timeout_and_cancellation_never_publish_result(ledger, monkeypatch):
    class Timeout:
        async def __aenter__(self): raise TimeoutError()
        async def __aexit__(self, *args): pass
    with monkeypatch.context() as m:
        m.setattr("app.google_connections_v22.ga4_comparison.asyncio.timeout", lambda seconds: Timeout())
        with pytest.raises(SyncError, match="SYNC_TIMEOUT"): await collect(ledger)
    async def cancelled(*args, **kwargs): raise asyncio.CancelledError()
    monkeypatch.setattr(Ga4ComparisonProvider, "_configured_events", cancelled)
    with pytest.raises(asyncio.CancelledError): await collect(ledger)
