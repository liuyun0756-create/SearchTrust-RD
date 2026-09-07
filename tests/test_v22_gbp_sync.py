import copy
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.google_connections_v22.gbp import (
    DAILY_METRICS, GBP_SCOPE,
    GbpProvider,
    SyncError,
    evaluate_health,
)
from app.google_connections_v22.broker_signature import sign_google_broker_body
from app.google_connections_v22.gbp_sync_worker import (
    cleanup_v22_gbp_content,
    execute_v22_gbp_sync,
    reconcile_v22_gbp_syncs,
)
from app.google_connections_v22.sync_io import SyncRepository, TokenBroker

END = date(2026, 9, 4)
LOCATION = "locations/12345"
FIXTURES = Path(__file__).parent / "fixtures/google_connections_v22/gbp"


def fixture(name: str):
    return json.loads((FIXTURES / name).read_text())


def google(request: httpx.Request):
    if request.url.host == "mybusinessbusinessinformation.googleapis.com":
        return httpx.Response(200, json=fixture("location_complete.json"))
    if request.url.path.endswith(":fetchMultiDailyMetricsTimeSeries"):
        return httpx.Response(200, json=fixture("performance_180_days.json"))
    return httpx.Response(200, json=fixture("keywords_page_1.json"))


async def collect(handler=google):
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        return await GbpProvider(client).collect(LOCATION, "fake-token", END)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_collects_only_bound_location_fixed_metrics_and_monthly_keywords():
    calls = []
    def capture(request):
        calls.append(request)
        return google(request)
    result = await collect(capture)
    assert len(calls) == 3
    location, performance, keywords = calls
    assert location.method == performance.method == keywords.method == "GET"
    assert location.url.path == "/v1/locations/12345"
    assert set(location.url.params["readMask"].split(",")) >= {"name", "title", "metadata", "serviceItems"}
    assert performance.url.path == "/v1/locations/12345:fetchMultiDailyMetricsTimeSeries"
    assert performance.url.params.get_list("dailyMetrics") == list(DAILY_METRICS)
    assert performance.url.params["dailyRange.start_date.year"] == "2026"
    assert performance.url.params["dailyRange.end_date.day"] == "4"
    assert keywords.url.path == "/v1/locations/12345/searchkeywords/impressions/monthly"
    assert keywords.url.params["pageSize"] == "100"
    assert result.snapshot.current.start_date == END - timedelta(days=89)
    assert result.snapshot.previous.start_date == END - timedelta(days=179)
    assert result.snapshot.current.impressions == 37
    assert result.snapshot.previous.impressions == 12
    assert result.snapshot.keywords.available and result.snapshot.keywords.threshold_applied
    assert result.snapshot.profile_checks.model_dump() == {
        "voice_of_merchant": True, "open": True, "title": True, "website": True,
        "phone": True, "primary_category": True, "regular_hours": True,
        "address_or_service_area": True,
    }
    assert evaluate_health(result.snapshot)[0] == "healthy"
    assert result.manifest()["current"]["has_impressions"] is True
    assert "Example Home Services" not in json.dumps(result.manifest())
    assert result.raw_payload["business_information"]["title"] == "Example Home Services"
    assert "fake-token" not in json.dumps(result.raw_payload)


@pytest.mark.anyio
async def test_keyword_pagination_is_bounded_and_thresholds_are_not_guessed():
    pages = []
    def paged(request):
        if "searchkeywords" not in request.url.path:
            return google(request)
        token = request.url.params.get("pageToken", "")
        pages.append(token)
        payload = {"searchKeywordsCounts": [{"searchKeyword": f"example term {len(pages)}", "insightsValue": {"threshold": "10"}}]}
        if len(pages) <= 10: payload["nextPageToken"] = f"page-{len(pages)}"
        return httpx.Response(200, json=payload)
    result = await collect(paged)
    assert len(pages) == 10
    assert result.snapshot.keywords.truncated
    assert "GBP_KEYWORDS_TRUNCATED" in result.snapshot.limitations
    assert result.snapshot.keyword_rows[0].threshold == 10
    assert result.snapshot.keyword_rows[0].value is None

    def loop(request):
        if "searchkeywords" in request.url.path:
            return httpx.Response(200, json={"searchKeywordsCounts": [], "nextPageToken": "same"})
        return google(request)
    with pytest.raises(SyncError, match="SYNC_INVALID_GOOGLE_RESPONSE"):
        await collect(loop)


@pytest.mark.anyio
@pytest.mark.parametrize("mutation", [
    lambda p: p.update({"name": "locations/999"}),
    lambda p: p.update({"websiteUri": "javascript:alert(1)"}),
    lambda p: p["metadata"].update({"hasVoiceOfMerchant": "true"}),
])
async def test_rejects_invalid_business_information(mutation):
    def invalid(request):
        if request.url.host == "mybusinessbusinessinformation.googleapis.com":
            payload = fixture("location_complete.json"); mutation(payload)
            return httpx.Response(200, json=payload)
        return google(request)
    with pytest.raises(SyncError, match="SYNC_INVALID_GOOGLE_RESPONSE"):
        await collect(invalid)


@pytest.mark.anyio
@pytest.mark.parametrize("mutation", [
    lambda p: p["multiDailyMetricTimeSeries"][0]["dailyMetricTimeSeries"][0]["timeSeries"]["datedValues"][0].update({"value": "-1"}),
    lambda p: p["multiDailyMetricTimeSeries"][0]["dailyMetricTimeSeries"][0]["timeSeries"]["datedValues"][0].update({"value": "9007199254740992"}),
    lambda p: p["multiDailyMetricTimeSeries"][0]["dailyMetricTimeSeries"].append(copy.deepcopy(p["multiDailyMetricTimeSeries"][0]["dailyMetricTimeSeries"][0])),
    lambda p: p["multiDailyMetricTimeSeries"][0]["dailyMetricTimeSeries"][0]["timeSeries"]["datedValues"][0].update({"date": {"year": 2020, "month": 1, "day": 1}}),
])
async def test_rejects_invalid_performance(mutation):
    def invalid(request):
        if request.url.path.endswith(":fetchMultiDailyMetricsTimeSeries"):
            payload = fixture("performance_180_days.json"); mutation(payload)
            return httpx.Response(200, json=payload)
        return google(request)
    with pytest.raises(SyncError, match="SYNC_INVALID_GOOGLE_RESPONSE"):
        await collect(invalid)


@pytest.mark.anyio
async def test_strict_health_reasons_and_nonfatal_coverage_reasons():
    result = await collect()
    checks = result.snapshot.profile_checks.model_copy(update={"website": False, "regular_hours": False})
    snapshot = result.snapshot.model_copy(update={"profile_checks": checks})
    health, reasons = evaluate_health(snapshot)
    assert health == "unhealthy"
    assert {"GBP_WEBSITE_MISSING", "GBP_REGULAR_HOURS_MISSING"} <= set(reasons)
    empty_current = result.snapshot.current.model_copy(update={"impressions": 0})
    empty_previous = result.snapshot.previous.model_copy(update={"impressions": 0})
    empty_keywords = result.snapshot.keywords.model_copy(update={"available": False})
    snapshot = result.snapshot.model_copy(update={"current": empty_current, "previous": empty_previous, "keywords": empty_keywords})
    health, reasons = evaluate_health(snapshot)
    assert health == "unhealthy"
    assert {"GBP_NO_CURRENT_IMPRESSIONS", "GBP_COMPARISON_UNAVAILABLE", "GBP_KEYWORDS_UNAVAILABLE"} <= set(reasons)


@pytest.mark.anyio
@pytest.mark.parametrize("status,code,retryable", [
    (401, "SYNC_GOOGLE_TOKEN_EXPIRED", True), (403, "SYNC_GOOGLE_ACCESS_DENIED", False),
    (404, "SYNC_GOOGLE_ACCESS_DENIED", False), (429, "SYNC_GOOGLE_UNAVAILABLE", True),
    (503, "SYNC_GOOGLE_UNAVAILABLE", True), (302, "SYNC_GOOGLE_REJECTED", False),
])
async def test_google_failures_use_fixed_secret_safe_codes(status, code, retryable):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(status, text="Bearer fake-secret"))) as client:
        with pytest.raises(SyncError) as error:
            await GbpProvider(client).collect(LOCATION, "fake-token", END)
    assert error.value.code == code and error.value.retryable == retryable
    assert "fake" not in str(error.value)


@pytest.mark.anyio
async def test_gbp_broker_signs_source_and_requires_exact_scope():
    secret, calls = "s" * 32, []

    def respond(request):
        calls.append(request)
        headers = request.headers
        assert headers["x-searchtrust-signature"] == sign_google_broker_body(
            secret,
            timestamp=int(headers["x-searchtrust-timestamp"]),
            request_id=headers["x-searchtrust-request-id"],
            nonce=headers["x-searchtrust-nonce"],
            body=request.content.decode(),
        )
        assert json.loads(request.content) == {
            "connection_id": "11111111-1111-4111-8111-111111111111",
            "purpose": "source_sync",
            "source": "gbp",
        }
        return httpx.Response(200, json={
            "access_token": "fake-access",
            "token_type": "Bearer",
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            "granted_scopes": [GBP_SCOPE],
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        token = await TokenBroker(
            "https://example.test", secret, client, source="gbp"
        ).access_token("11111111-1111-4111-8111-111111111111")

    assert token == "fake-access" and len(calls) == 1


def worker_context():
    job = {
        "id": "11111111-1111-4111-8111-111111111111",
        "lease_id": "22222222-2222-4222-8222-222222222222",
        "connection_id": "33333333-3333-4333-8333-333333333333",
        "coverage_end": END.isoformat(),
        "resource_id": LOCATION,
    }
    repository = SimpleNamespace(
        claim=AsyncMock(return_value=job),
        finish=AsyncMock(),
        fail=AsyncMock(),
        pending=AsyncMock(return_value=[job["id"]]),
    )
    return {
        "gbp_sync_repository": repository,
        "gbp_token_broker": SimpleNamespace(
            access_token=AsyncMock(return_value="fake-access")
        ),
        "gbp_provider": SimpleNamespace(collect=AsyncMock()),
        "redis": SimpleNamespace(enqueue_job=AsyncMock()),
        "gbp_queue_name": "private-test",
    }


@pytest.mark.anyio
async def test_worker_claims_before_token_and_separates_manifest_from_expiring_content():
    ctx = worker_context()
    collection = await collect()
    ctx["gbp_provider"].collect.return_value = collection

    await execute_v22_gbp_sync(ctx, ctx["gbp_sync_repository"].claim.return_value["id"])

    call = ctx["gbp_sync_repository"].finish.call_args
    assert call.args[1] == collection.manifest()
    assert call.args[2].startswith("sha256:")
    assert call.args[3] == "healthy"
    assert call.kwargs["raw_payload"] == collection.raw_payload
    assert "Example Home Services" not in json.dumps(call.args[1])
    assert "fake-access" not in json.dumps(call.args) + json.dumps(call.kwargs)

    ctx["gbp_sync_repository"].claim.return_value = None
    ctx["gbp_token_broker"].access_token.reset_mock()
    await execute_v22_gbp_sync(ctx, "11111111-1111-4111-8111-111111111111")
    ctx["gbp_token_broker"].access_token.assert_not_called()


@pytest.mark.anyio
async def test_gbp_dispatch_repository_and_cleanup_are_source_scoped_and_secret_safe(caplog):
    await reconcile_v22_gbp_syncs({})
    ctx = worker_context()
    await reconcile_v22_gbp_syncs(ctx)
    ctx["redis"].enqueue_job.assert_awaited_once_with(
        "execute_v22_gbp_sync",
        "11111111-1111-4111-8111-111111111111",
        _job_id="gbp-sync:11111111-1111-4111-8111-111111111111",
        _queue_name="private-test",
    )

    ctx["gbp_provider"].collect.side_effect = RuntimeError("Bearer fake-secret")
    await execute_v22_gbp_sync(ctx, "11111111-1111-4111-8111-111111111111")
    assert ctx["gbp_sync_repository"].fail.call_args.args[1].code == "SYNC_FAILED"
    assert "fake-secret" not in caplog.text

    calls = []

    def respond(request):
        calls.append(request)
        if request.method == "GET":
            return httpx.Response(200, json=[{"id": "11111111-1111-4111-8111-111111111111"}])
        if request.url.path.endswith("/rpc/cleanup_v22_expired_gbp_content"):
            return httpx.Response(200, json=3)
        return httpx.Response(200, json=None)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        repository = SyncRepository(
            "https://project.supabase.co", "fake-service", client, source="gbp"
        )
        assert await repository.pending() == ["11111111-1111-4111-8111-111111111111"]
        assert await repository.claim("11111111-1111-4111-8111-111111111111") is None
        await repository.finish(
            {"id": "job", "lease_id": "lease"},
            {"schema_version": "gbp_sync_v1"},
            "sha256:" + "a" * 64,
            "healthy",
            [],
            raw_payload={"business_information": {"title": "Example"}},
        )
        assert await repository.cleanup_expired(batch_size=25) == 3

    assert calls[0].url.params["source_type"] == "eq.gbp"
    assert calls[1].url.path.endswith("/rpc/claim_v22_gbp_sync")
    finish_body = json.loads(calls[2].content)
    assert set(finish_body) == {
        "p_job_id", "p_lease_id", "p_manifest", "p_raw_payload",
        "p_checksum", "p_health", "p_reasons",
    }
    cleanup_body = json.loads(calls[3].content)
    assert cleanup_body["p_batch_size"] == 25
    assert datetime.fromisoformat(cleanup_body["p_now"]).tzinfo is not None

    cleanup_repository = SimpleNamespace(cleanup_expired=AsyncMock(return_value=2))
    await cleanup_v22_gbp_content({"gbp_cleanup_repository": cleanup_repository})
    cleanup_repository.cleanup_expired.assert_awaited_once_with()
