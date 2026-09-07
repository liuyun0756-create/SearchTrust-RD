import copy
import json
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.google_connections_v22.ga4 import (
    Ga4Provider, SyncError, evaluate_health, normalize_report,
)
from app.google_connections_v22.ga4_sync_worker import execute_v22_ga4_sync, reconcile_v22_ga4_syncs
from app.google_connections_v22.sync_io import SyncRepository, TokenBroker
from app.google_connections_v22.broker_signature import sign_google_broker_body

END = date(2026, 9, 5)
PROPERTY = "properties/12345"
HOSTS = ["example.com", "www.example.com"]


@pytest.fixture
def anyio_backend():
    return "asyncio"


def value(name: str) -> str:
    return {
        "sessions": "10", "activeUsers": "8", "engagedSessions": "7",
        "engagementRate": "0.7", "averageSessionDuration": "42.5",
        "screenPageViews": "15", "keyEvents": "2", "sessionKeyEventRate": "0.2",
    }[name]


def report_payload(body: dict, *, metadata=None, rows=True):
    dimensions = [item["name"] for item in body["dimensions"]]
    metrics = [item["name"] for item in body["metrics"]]
    keys = {"landingPage": "/service", "date": body["dateRanges"][0]["endDate"].replace("-", ""), "eventName": "generate_lead"}
    result = {
        "dimensionHeaders": [{"name": item} for item in dimensions],
        "metricHeaders": [{"name": item, "type": "TYPE_FLOAT" if item in {"engagementRate", "averageSessionDuration", "keyEvents", "sessionKeyEventRate"} else "TYPE_INTEGER"} for item in metrics],
        "rows": [{"dimensionValues": [{"value": keys[item]} for item in dimensions],
                  "metricValues": [{"value": value(item)} for item in metrics]}] if rows else [],
        "rowCount": 1 if rows else 0,
        "metadata": {"timeZone": "America/Chicago", **(metadata or {})},
    }
    return result


def google(request: httpx.Request):
    if request.method == "GET":
        return httpx.Response(200, json={"keyEvents": [{"name": f"{PROPERTY}/keyEvents/1", "eventName": "generate_lead",
            "countingMethod": "ONCE_PER_EVENT", "custom": True, "deletable": True,
            "createTime": "2026-01-01T00:00:00Z"}]})
    body = json.loads(request.content)
    return httpx.Response(200, json=report_payload(body))


async def snapshot(handler=google):
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        return await Ga4Provider(client).collect(PROPERTY, HOSTS, "fake-token", END)


@pytest.mark.anyio
async def test_fixed_property_hosts_windows_and_eight_bounded_reports():
    calls = []
    def capture(request):
        calls.append(request)
        return google(request)
    result = await snapshot(capture)
    assert len(calls) == 9
    assert calls[0].method == "GET" and calls[0].url.host == "analyticsadmin.googleapis.com"
    assert all(call.url.host == "analyticsdata.googleapis.com" for call in calls[1:])
    assert result.current.start_date == END - timedelta(days=89)
    assert result.previous.end_date == END - timedelta(days=90)
    assert result.previous.start_date == END - timedelta(days=179)
    for call in calls[1:]:
        body = json.loads(call.content)
        filters = body["dimensionFilter"]["andGroup"]["expressions"]
        assert filters[0]["filter"]["fieldName"] == "platform"
        assert filters[1]["filter"]["inListFilter"]["values"] == HOSTS
        assert body["keepEmptyRows"] is False and body["offset"] == "0"
        dimension = body["dimensions"][0]["name"] if body["dimensions"] else None
        assert body["limit"] == {None: "1", "landingPage": "251", "date": "90", "eventName": "101"}[dimension]
        if dimension == "landingPage": assert body["orderBys"] == [{"metric": {"metricName": "sessions"}, "desc": True}]
        if dimension == "date": assert body["orderBys"] == [{"dimension": {"dimensionName": "date"}, "desc": False}]
        if dimension == "eventName": assert body["orderBys"] == [{"metric": {"metricName": "keyEvents"}, "desc": True}]
    assert result.current.totals.rows[0].sessions == 10
    assert result.current.landing_pages.rows[0].landing_page == "/service"
    assert result.configured_key_events[0].event_name == "generate_lead"
    assert "fake-token" not in result.model_dump_json()
    assert evaluate_health(result)[0] == "healthy"


@pytest.mark.anyio
async def test_key_event_admin_pagination_is_bounded_and_loop_safe():
    calls = []
    def pages(request):
        calls.append(request)
        if request.method == "GET":
            page = request.url.params.get("pageToken")
            return httpx.Response(200, json={"keyEvents": [] if page else [{"name": f"{PROPERTY}/keyEvents/2", "eventName": "lead", "countingMethod": "ONCE_PER_SESSION", "custom": True, "deletable": True}],
                **({} if page else {"nextPageToken": "next"})})
        return httpx.Response(200, json=report_payload(json.loads(request.content)))
    result = await snapshot(pages)
    assert [call.url.params.get("pageToken") for call in calls if call.method == "GET"] == ["", "next"]
    assert [event.event_name for event in result.configured_key_events] == ["lead"]
    def loop(request):
        if request.method == "GET": return httpx.Response(200, json={"keyEvents": [], "nextPageToken": "same"})
        return google(request)
    with pytest.raises(SyncError, match="SYNC_INVALID_GOOGLE_RESPONSE"):
        await snapshot(loop)


@pytest.mark.anyio
async def test_metadata_limitations_and_top_row_caps_are_explicit():
    def limited(request):
        if request.method == "GET": return google(request)
        body = json.loads(request.content)
        payload = report_payload(body, metadata={"subjectToThresholding": True, "dataLossFromOtherRow": True,
            "samplingMetadatas": [{"samplesReadCount": "50", "samplingSpaceSize": "100"}],
            "schemaRestrictionResponse": {"activeMetricRestrictions": [{"metricName": "keyEvents", "restrictedMetricTypes": ["REVENUE_DATA"]}]}})
        dimension = body["dimensions"][0]["name"] if body["dimensions"] else None
        if dimension in {"landingPage", "eventName"}:
            limit = 250 if dimension == "landingPage" else 100
            payload["rows"] = [copy.deepcopy(payload["rows"][0]) for _ in range(limit + 1)]
            for index, row in enumerate(payload["rows"]): row["dimensionValues"][0]["value"] = f"/{index}" if dimension == "landingPage" else f"event_{index}"
            payload["rowCount"] = limit + 10
        return httpx.Response(200, json=payload)
    result = await snapshot(limited)
    health, reasons = evaluate_health(result)
    assert health == "healthy"
    assert len(result.current.landing_pages.rows) == 250 and result.current.landing_pages.truncated
    assert len(result.current.key_events.rows) == 100 and result.current.key_events.truncated
    assert {"GA4_SAMPLED_DATA", "GA4_THRESHOLDING_APPLIED", "GA4_OTHER_ROW_DATA_LOSS", "GA4_METRIC_RESTRICTIONS",
            "GA4_LANDING_PAGE_TRUNCATED", "GA4_KEY_EVENT_TRUNCATED"} <= set(reasons)


@pytest.mark.anyio
async def test_no_configured_events_is_unhealthy_but_configured_zero_is_warning():
    def empty_config(request):
        if request.method == "GET": return httpx.Response(200, json={})
        return google(request)
    result = await snapshot(empty_config)
    assert evaluate_health(result)[0] == "unhealthy"
    assert "GA4_NO_CONFIGURED_KEY_EVENTS" in evaluate_health(result)[1]
    result = await snapshot()
    result.current.totals.rows[0].key_events = 0
    health, reasons = evaluate_health(result)
    assert health == "healthy" and "GA4_CONFIGURED_KEY_EVENTS_NO_ACTIVITY" in reasons


@pytest.mark.anyio
async def test_empty_current_and_activity_gaps_are_reviewed_without_claiming_code_failure():
    result = await snapshot()
    result.current.dates.rows[0].date = END - timedelta(days=30)
    health, reasons = evaluate_health(result)
    assert health == "unhealthy" and "GA4_RECENT_ACTIVITY_MISSING_REVIEW" in reasons
    result = await snapshot()
    earlier = result.current.dates.rows[0].model_copy(update={"date": END - timedelta(days=30)})
    result.current.dates.rows.insert(0, earlier)
    assert "GA4_ACTIVITY_GAP_REVIEW" in evaluate_health(result)[1]
    result = await snapshot()
    result.current.totals.rows.clear(); result.current.landing_pages.rows.clear(); result.current.dates.rows.clear()
    assert {"GA4_NO_CURRENT_SESSIONS", "GA4_NO_LANDING_PAGE_ROWS", "GA4_NO_ACTIVITY_DATES"} <= set(evaluate_health(result)[1])


def valid_payload(view="landing_pages"):
    dimensions, metrics, _ = {
        "totals": ([], ["sessions", "activeUsers", "engagedSessions", "engagementRate", "averageSessionDuration", "screenPageViews", "keyEvents", "sessionKeyEventRate"], 1),
        "landing_pages": (["landingPage"], ["sessions", "engagedSessions", "engagementRate", "averageSessionDuration", "screenPageViews", "keyEvents", "sessionKeyEventRate"], 250),
    }[view]
    body = {"dimensions": [{"name": item} for item in dimensions], "metrics": [{"name": item} for item in metrics],
            "dateRanges": [{"endDate": END.isoformat()}]}
    return report_payload(body)


@pytest.mark.parametrize("mutation", [
    lambda p: p.update({"dimensionHeaders": [{"name": "pageLocation"}]}),
    lambda p: p["rows"][0]["metricValues"].__setitem__(0, {"value": "-1"}),
    lambda p: p["rows"][0]["metricValues"].__setitem__(0, {"value": "9007199254740992"}),
    lambda p: p["rows"][0]["metricValues"].__setitem__(3, {"value": "NaN"}),
    lambda p: p.update({"metadata": {"timeZone": "bad\nzone"}}),
    lambda p: p.update({"rowCount": 0}),
])
def test_rejects_malformed_or_unsafe_reports(mutation):
    payload = valid_payload()
    mutation(payload)
    with pytest.raises(SyncError, match="SYNC_INVALID_GOOGLE_RESPONSE"):
        normalize_report(payload, view="landing_pages", start=END-timedelta(days=89), end=END)


@pytest.mark.anyio
@pytest.mark.parametrize("status,code,retryable", [
    (401, "SYNC_GOOGLE_TOKEN_EXPIRED", True), (403, "SYNC_GOOGLE_ACCESS_DENIED", False),
    (404, "SYNC_GOOGLE_ACCESS_DENIED", False), (429, "SYNC_GOOGLE_UNAVAILABLE", True),
    (503, "SYNC_GOOGLE_UNAVAILABLE", True), (302, "SYNC_GOOGLE_REJECTED", False),
])
async def test_google_failures_use_fixed_codes(status, code, retryable):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(status, text="Bearer fake-secret"))) as client:
        with pytest.raises(SyncError) as error:
            await Ga4Provider(client).collect(PROPERTY, HOSTS, "fake-token", END)
    assert error.value.code == code and error.value.retryable == retryable
    assert "fake" not in str(error.value)


@pytest.mark.anyio
async def test_quota_and_response_size_are_bounded():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(403, json={"error": {"errors": [{"reason": "quotaExceeded", "message": "secret"}]}}))) as client:
        with pytest.raises(SyncError) as error:
            await Ga4Provider(client).collect(PROPERTY, HOSTS, "fake", END)
        assert error.value.retryable
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"x" * 2_000_001))) as client:
        with pytest.raises(SyncError, match="SYNC_RESPONSE_TOO_LARGE"):
            await Ga4Provider(client).collect(PROPERTY, HOSTS, "fake", END)


@pytest.mark.anyio
@pytest.mark.parametrize("end", [date(2024, 3, 10), date(2026, 11, 1), date(2027, 1, 1)])
async def test_calendar_windows_remain_exact_across_dst_leap_and_year_boundaries(end):
    async with httpx.AsyncClient(transport=httpx.MockTransport(google)) as client:
        result = await Ga4Provider(client).collect(PROPERTY, HOSTS, "fake", end)
    assert (result.current.end_date-result.current.start_date).days == 89
    assert result.current.start_date-result.previous.end_date == timedelta(days=1)
    assert (result.previous.end_date-result.previous.start_date).days == 89


@pytest.mark.anyio
async def test_ga4_broker_signs_source_and_requires_exact_scope():
    secret, calls = "s" * 32, []
    def respond(request):
        calls.append(request)
        headers = request.headers
        assert headers["x-searchtrust-signature"] == sign_google_broker_body(secret,
            timestamp=int(headers["x-searchtrust-timestamp"]), request_id=headers["x-searchtrust-request-id"],
            nonce=headers["x-searchtrust-nonce"], body=request.content.decode())
        assert json.loads(request.content) == {"connection_id": "11111111-1111-4111-8111-111111111111",
            "purpose": "source_sync", "source": "ga4"}
        return httpx.Response(200, json={"access_token": "fake-access", "token_type": "Bearer",
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            "granted_scopes": ["https://www.googleapis.com/auth/analytics.readonly"]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        token = await TokenBroker("https://example.test", secret, client, source="ga4").access_token("11111111-1111-4111-8111-111111111111")
    assert token == "fake-access" and len(calls) == 1


def worker_context():
    job = {"id": "11111111-1111-4111-8111-111111111111", "lease_id": "11111111-1111-4111-8111-111111111111",
        "connection_id": "11111111-1111-4111-8111-111111111111", "coverage_end": END.isoformat(),
        "resource_id": PROPERTY, "filter_hosts": HOSTS}
    repo = SimpleNamespace(claim=AsyncMock(return_value=job), finish=AsyncMock(), fail=AsyncMock(),
        pending=AsyncMock(return_value=[job["id"]]))
    return {"ga4_sync_repository": repo, "ga4_token_broker": SimpleNamespace(access_token=AsyncMock(return_value="fake-access")),
        "ga4_provider": SimpleNamespace(collect=AsyncMock()), "redis": SimpleNamespace(enqueue_job=AsyncMock()),
        "ga4_queue_name": "private-test"}


@pytest.mark.anyio
async def test_worker_claims_before_token_and_persists_only_normalized_snapshot():
    ctx = worker_context()
    ctx["ga4_provider"].collect.return_value = await snapshot()
    await execute_v22_ga4_sync(ctx, ctx["ga4_sync_repository"].claim.return_value["id"])
    args = ctx["ga4_sync_repository"].finish.call_args.args
    assert args[1]["schema_version"] == "ga4_sync_v1" and args[2].startswith("sha256:") and args[3] == "healthy"
    assert "fake-access" not in json.dumps(args)
    ctx["ga4_sync_repository"].claim.return_value = None
    ctx["ga4_token_broker"].access_token.reset_mock()
    await execute_v22_ga4_sync(ctx, "11111111-1111-4111-8111-111111111111")
    ctx["ga4_token_broker"].access_token.assert_not_called()


@pytest.mark.anyio
async def test_ga4_dispatch_and_repository_are_source_scoped_and_secret_safe(caplog):
    await reconcile_v22_ga4_syncs({})
    ctx = worker_context()
    await reconcile_v22_ga4_syncs(ctx)
    ctx["redis"].enqueue_job.assert_awaited_once_with("execute_v22_ga4_sync", "11111111-1111-4111-8111-111111111111",
        _job_id="ga4-sync:11111111-1111-4111-8111-111111111111", _queue_name="private-test")
    ctx["ga4_provider"].collect.side_effect = RuntimeError("Bearer fake-secret")
    await execute_v22_ga4_sync(ctx, "11111111-1111-4111-8111-111111111111")
    assert ctx["ga4_sync_repository"].fail.call_args.args[1].code == "SYNC_FAILED"
    assert "fake-secret" not in caplog.text
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(200, json=[{"id": "11111111-1111-4111-8111-111111111111"}] if request.method == "GET" else None)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        repository = SyncRepository("https://project.supabase.co", "fake-service", client, source="ga4")
        assert await repository.pending() == ["11111111-1111-4111-8111-111111111111"]
        assert await repository.claim("11111111-1111-4111-8111-111111111111") is None
    assert calls[0].url.params["source_type"] == "eq.ga4"
    assert calls[1].url.path.endswith("/rpc/claim_v22_ga4_sync")
