import asyncio
import json
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import fakeredis.aioredis
import pytest

from app.google_connections_v22.gsc import GscProvider, SyncError, evaluate_health, normalize_view
from app.google_connections_v22.sync_io import SyncRepository, TokenBroker
from app.google_connections_v22.sync_worker import execute_v22_gsc_sync, reconcile_v22_gsc_syncs
from app.google_connections_v22.broker_signature import sign_google_broker_body
from app.jobs_v22.cost_ledger import JobCostLedger
from app.jobs_v22.cost_models import PricingCatalog

ID = "11111111-1111-4111-8111-111111111111"
END = date(2026, 9, 1)


@pytest.fixture
def anyio_backend():
    return "asyncio"


def row(key=None, **values):
    return {"keys": [key] if key is not None else [], "clicks": 2, "impressions": 20, "ctr": 0.1, "position": 3, **values}


def handler(request):
    body = json.loads(request.content)
    dimension = next(iter(body["dimensions"]), None)
    key = {"query": "local plumbing", "page": "https://example.com/", "date": body["endDate"], "country": "usa", "device": "MOBILE"}.get(dimension)
    return httpx.Response(200, json={"rows": [row(key)], "responseAggregationType": "byPage" if dimension == "page" else "byProperty"})


async def snapshot():
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        return await GscProvider(client).collect("sc-domain:example.com", "fake-access-token", END)


@pytest.mark.anyio
async def test_exact_windows_views_totals_and_private_fixed_endpoint():
    calls = []
    def capture(request):
        calls.append(request)
        return handler(request)
    async with httpx.AsyncClient(transport=httpx.MockTransport(capture)) as client:
        result = await GscProvider(client).collect("https://example.com/shop/", "fake-token", END)
    assert len(calls) == 12
    assert result.current.start_date == END - timedelta(days=89)
    assert result.previous.end_date == result.current.start_date - timedelta(days=1)
    assert result.previous.start_date == END - timedelta(days=179)
    assert all(r.url.host == "www.googleapis.com" for r in calls)
    assert b"https%3A%2F%2Fexample.com%2Fshop%2F" in calls[0].url.raw_path
    assert all(json.loads(r.content)["dataState"] == "final" for r in calls)
    assert result.current.totals.rows[0].clicks == 2  # not 2*6 overlapping views
    assert result.current.pages.aggregation_type == "byPage"
    assert "fake-token" not in result.model_dump_json()
    health, reasons = evaluate_health(result)
    assert health == "healthy"
    assert "GSC_QUERY_PRIVACY_FILTERING" in reasons


@pytest.mark.anyio
async def test_gsc_cost_ledger_counts_twelve_real_google_requests() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    ledger = JobCostLedger(
        redis,
        prefix="test:v22",
        job_id=__import__("uuid").UUID(ID),
        ttl_seconds=604_800,
        pricing=PricingCatalog(),
        job_created_at=datetime.now(timezone.utc),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await GscProvider(client).collect(
            "sc-domain:example.com", "fake-token", END, cost_ledger=ledger
        )
    counters = (await ledger.snapshot()).root
    assert counters["gsc_attempts"] == 12
    assert counters["gsc_successes"] == 12


@pytest.mark.anyio
async def test_detail_cap_is_1000_for_both_periods_not_a_completeness_claim():
    def many(request):
        body = json.loads(request.content)
        if body["dimensions"] in (["query"], ["page"]):
            return httpx.Response(200, json={"rows": [row(f"key-{i}") for i in range(251)]})
        return handler(request)
    async with httpx.AsyncClient(transport=httpx.MockTransport(many)) as client:
        result = await GscProvider(client).collect("sc-domain:example.com", "fake", END)
    assert sum(len(getattr(p, view).rows) for p in (result.current, result.previous) for view in ("queries", "pages")) == 1000
    assert "GSC_DETAIL_TRUNCATED" in result.limitations
    assert evaluate_health(result)[0] == "healthy"


@pytest.mark.parametrize("payload", [None, [], {"rows": None}, {"rows": [row("x", clicks=-1)]},
    {"rows": [row("x", ctr=1.1)]}, {"rows": [row("x", impressions=float("nan"))]},
    {"rows": [row("x", clicks=True)]}, {"rows": [row("x"), row("x")]},
    {"rows": [row(None)]}, {"rows": [row("x")], "responseAggregationType": "other"}])
def test_rejects_malformed_google_rows_without_raw_values(payload):
    with pytest.raises(SyncError, match="SYNC_INVALID_GOOGLE_RESPONSE"):
        normalize_view(payload, dimension="query", limit=250, start=END-timedelta(days=89), end=END)


@pytest.mark.parametrize("dimension,key", [("date", "2026-99-01"), ("date", "2027-01-01"), ("device", "TV"), ("country", "US")])
def test_validates_breakdown_keys(dimension, key):
    with pytest.raises(SyncError):
        normalize_view({"rows": [row(key)]}, dimension=dimension, limit=90, start=END-timedelta(days=89), end=END)


@pytest.mark.anyio
async def test_empty_current_is_unhealthy_but_empty_previous_is_only_a_limitation():
    def empty_previous(request):
        if json.loads(request.content)["endDate"] != END.isoformat():
            return httpx.Response(200, json={})
        return handler(request)
    async with httpx.AsyncClient(transport=httpx.MockTransport(empty_previous)) as client:
        result = await GscProvider(client).collect("sc-domain:example.com", "fake", END)
    assert evaluate_health(result)[0] == "healthy"
    assert "GSC_COMPARISON_UNAVAILABLE" in evaluate_health(result)[1]
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={}))) as client:
        result = await GscProvider(client).collect("sc-domain:example.com", "fake", END)
    assert evaluate_health(result)[0] == "unhealthy"
    assert "GSC_NO_CURRENT_DATA" in evaluate_health(result)[1]


@pytest.mark.anyio
async def test_gaps_and_stale_activity_require_review_without_claiming_tracking_failure():
    result = await snapshot()
    result.current.dates = normalize_view({"rows": [row((END-timedelta(days=30)).isoformat()), row(END.isoformat())]},
        dimension="date", limit=90, start=result.current.start_date, end=END)
    assert "GSC_ACTIVITY_GAP_REVIEW" in evaluate_health(result)[1]
    result.current.dates.rows.pop()
    assert "GSC_RECENT_ACTIVITY_MISSING_REVIEW" in evaluate_health(result)[1]


@pytest.mark.anyio
@pytest.mark.parametrize("status,code,retryable", [(401,"SYNC_GOOGLE_TOKEN_EXPIRED",True), (403,"SYNC_GOOGLE_ACCESS_DENIED",False), (404,"SYNC_GOOGLE_ACCESS_DENIED",False),
    (429,"SYNC_GOOGLE_UNAVAILABLE",True), (503,"SYNC_GOOGLE_UNAVAILABLE",True), (302,"SYNC_GOOGLE_REJECTED",False)])
async def test_error_classification_is_safe(status, code, retryable):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(status, text="fake-secret"))) as client:
        with pytest.raises(SyncError) as error:
            await GscProvider(client).collect("sc-domain:example.com", "fake-token", END)
    assert error.value.code == code and error.value.retryable == retryable
    assert "fake" not in str(error.value)


@pytest.mark.anyio
async def test_403_quota_is_retryable_and_responses_are_bounded():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(403, json={"error": {"errors": [{"reason": "rateLimitExceeded", "message": "secret"}]}}))) as client:
        with pytest.raises(SyncError) as error:
            await GscProvider(client).collect("sc-domain:example.com", "fake", END)
        assert error.value.retryable
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"x"*2_000_001))) as client:
        with pytest.raises(SyncError, match="SYNC_RESPONSE_TOO_LARGE"):
            await GscProvider(client).collect("sc-domain:example.com", "fake", END)


@pytest.mark.anyio
async def test_broker_signs_exact_body_and_fresh_nonce_without_refresh_token():
    calls = []
    secret = "s"*32
    def respond(request):
        calls.append(request)
        h = request.headers
        assert h["x-searchtrust-signature"] == sign_google_broker_body(secret, timestamp=int(h["x-searchtrust-timestamp"]),
            request_id=h["x-searchtrust-request-id"], nonce=h["x-searchtrust-nonce"], body=request.content.decode())
        assert json.loads(request.content) == {"connection_id": ID, "purpose": "source_sync", "source": "gsc"}
        return httpx.Response(200, json={"access_token": "fake-access", "token_type": "Bearer",
            "expires_at": (datetime.now(timezone.utc)+timedelta(hours=1)).isoformat(),
            "granted_scopes": ["https://www.googleapis.com/auth/webmasters.readonly"]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        broker = TokenBroker("https://example.test", secret, client)
        assert await broker.access_token(ID) == "fake-access"
        await broker.access_token(ID)
    assert calls[0].headers["x-searchtrust-nonce"] != calls[1].headers["x-searchtrust-nonce"]


@pytest.mark.anyio
async def test_broker_rejects_missing_scope_and_never_follows_redirects():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"access_token": "fake"}))) as client:
        with pytest.raises(SyncError, match="SYNC_INVALID_BROKER_RESPONSE"):
            await TokenBroker("https://example.test", "s"*32, client).access_token(ID)
    with pytest.raises(SyncError):
        TokenBroker("http://example.test", "s"*32, None)


@pytest.mark.anyio
@pytest.mark.parametrize("change", [{"granted_scopes": "https://www.googleapis.com/auth/webmasters.readonly"},
    {"expires_at": "2000-01-01T00:00:00Z"}, {"expires_at": "2030-01-01T00:00:00"}, {"access_token": "bad\r\ntoken"}])
async def test_broker_validates_expiry_scope_shape_and_header_safety(change):
    payload = {"access_token": "fake-access", "token_type": "Bearer", "expires_at": "2030-01-01T00:00:00Z",
               "granted_scopes": ["https://www.googleapis.com/auth/webmasters.readonly"], **change}
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))) as client:
        with pytest.raises(SyncError, match="SYNC_INVALID_BROKER_RESPONSE"):
            await TokenBroker("https://example.test", "s"*32, client).access_token(ID)


@pytest.mark.anyio
@pytest.mark.parametrize("end", [date(2024, 3, 10), date(2026, 11, 1), date(2027, 1, 1)])
async def test_windows_remain_90_calendar_days_across_leap_dst_and_year_boundaries(end):
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await GscProvider(client).collect("sc-domain:example.com", "fake", end)
    assert (result.current.end_date - result.current.start_date).days == 89
    assert (result.previous.end_date - result.previous.start_date).days == 89
    assert result.current.start_date - result.previous.end_date == timedelta(days=1)


def worker_context():
    job = {"id": ID, "lease_id": ID, "connection_id": ID, "coverage_end": END.isoformat(), "resource_id": "sc-domain:example.com"}
    repo = SimpleNamespace(claim=AsyncMock(return_value=job), finish=AsyncMock(), fail=AsyncMock(), pending=AsyncMock(return_value=[ID]))
    return {"gsc_sync_repository": repo, "gsc_token_broker": SimpleNamespace(access_token=AsyncMock(return_value="fake-access")),
            "gsc_provider": SimpleNamespace(collect=AsyncMock()), "redis": SimpleNamespace(enqueue_job=AsyncMock()), "gsc_queue_name": "private-test"}


@pytest.mark.anyio
async def test_worker_claims_before_token_and_persists_normalized_snapshot():
    ctx = worker_context()
    ctx["gsc_provider"].collect.return_value = await snapshot()
    await execute_v22_gsc_sync(ctx, ID)
    args = ctx["gsc_sync_repository"].finish.call_args.args
    assert args[1]["schema_version"] == "gsc_sync_v1" and args[2].startswith("sha256:")
    assert "fake-access" not in json.dumps(args)
    assert args[3] == "healthy"
    ctx["gsc_sync_repository"].fail.assert_not_called()
    ctx["gsc_sync_repository"].claim.return_value = None
    ctx["gsc_token_broker"].access_token.reset_mock()
    await execute_v22_gsc_sync(ctx, ID)
    ctx["gsc_token_broker"].access_token.assert_not_called()


@pytest.mark.anyio
async def test_worker_passes_complete_private_cost_snapshot_to_terminal_rpc() -> None:
    ctx = worker_context()
    ctx["redis"] = fakeredis.aioredis.FakeRedis(decode_responses=False)
    ctx["cost_pricing"] = PricingCatalog()
    ctx["state_ttl_seconds"] = 604_800
    ctx["gsc_sync_repository"].claim.return_value["created_at"] = datetime.now(
        timezone.utc
    ).isoformat()
    ctx["gsc_provider"].collect.return_value = await snapshot()

    await execute_v22_gsc_sync(ctx, ID)

    counters = ctx["gsc_sync_repository"].finish.call_args.kwargs["cost_counters"]
    assert counters["cost_schema_version"] == 1
    assert counters["job_attempts"] == 1
    assert counters["gsc_attempts"] == 0


@pytest.mark.anyio
async def test_worker_failure_preserves_snapshot_and_logs_only_fixed_codes(caplog):
    ctx = worker_context()
    ctx["gsc_provider"].collect.side_effect = RuntimeError("Bearer fake-secret")
    await execute_v22_gsc_sync(ctx, ID)
    ctx["gsc_sync_repository"].finish.assert_not_called()
    assert ctx["gsc_sync_repository"].fail.call_args.args[1].code == "SYNC_FAILED"
    assert "fake-secret" not in caplog.text
    ctx["gsc_provider"].collect.side_effect = asyncio.CancelledError()
    ctx["gsc_sync_repository"].fail.reset_mock()
    with pytest.raises(asyncio.CancelledError):
        await execute_v22_gsc_sync(ctx, ID)
    ctx["gsc_sync_repository"].fail.assert_not_called()


@pytest.mark.anyio
async def test_dispatcher_has_no_tokens_in_queue_and_disabled_is_noop():
    await reconcile_v22_gsc_syncs({})
    await execute_v22_gsc_sync({}, ID)
    ctx = worker_context()
    await reconcile_v22_gsc_syncs(ctx)
    ctx["redis"].enqueue_job.assert_awaited_once_with("execute_v22_gsc_sync", ID, _job_id=f"gsc-sync:{ID}", _queue_name="private-test")


@pytest.mark.anyio
async def test_repository_selects_only_pending_jobs_and_uses_fenced_rpcs():
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(200, json=[{"id": ID}] if request.method == "GET" else None)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        repo = SyncRepository("https://project.supabase.co", "fake-service", client)
        assert await repo.pending() == [ID]
        assert await repo.claim(ID) is None
    assert calls[0].url.params["select"] == "id"
    assert "lease_expires_at" in calls[0].url.params["or"]
    assert json.loads(calls[1].content) == {"p_job_id": ID}
