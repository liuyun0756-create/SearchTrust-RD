import json
from datetime import timedelta
from pathlib import Path
from uuid import UUID

import fakeredis.aioredis
import httpx
import pytest
from pydantic import SecretStr
from redis.exceptions import RedisError

from app.api.v2.runtime import V22JobRuntime
from app.competitors_v22.selection import AnalysisDiscoveryLink, DiscoverySelectionError
from app.core.config import settings
from app.jobs_v22.models import JobErrorState, utc_now
from app.jobs_v22.store import DurableJobStore
from app.main import create_app


JOB_ID = UUID("55555555-5555-4555-8555-555555555555")
CASE_ID = UUID("11111111-1111-4111-8111-111111111111")
DISCOVERY_ID = UUID("22222222-2222-4222-8222-222222222222")
MARKET_ID = UUID("44444444-4444-4444-8444-444444444444")
DIGEST = "sha256:" + "a" * 64
CONTRACT_DIR = Path(__file__).resolve().parents[1] / "contracts" / "v2.2"
AUTH_HEADERS = {
    "Authorization": "Bearer test-internal-token",
    "X-SearchTrust-Job-ID": str(JOB_ID),
    "X-SearchTrust-Discovery-ID": str(DISCOVERY_ID),
    "Idempotency-Key": "generation-intent-1",
}
VERIFIED_JOB_ID = UUID("77777777-7777-4777-8777-777777777777")
VERIFIED_HEADERS = {
    "Authorization": "Bearer test-internal-token",
    "X-SearchTrust-Job-ID": str(VERIFIED_JOB_ID),
    "Idempotency-Key": "verified-generation-intent-1",
}


class RecordingQueue:
    def __init__(self) -> None:
        self.calls: list[tuple[UUID, int]] = []

    async def enqueue(self, job_id: UUID, run_generation: int) -> bool:
        call = (job_id, run_generation)
        if call in self.calls:
            return False
        self.calls.append(call)
        return True


class RecordingDiscoveryVerifier:
    async def verify(self, *, discovery_id, request, now):
        if discovery_id is None:
            raise DiscoverySelectionError(
                "COMPETITOR_DISCOVERY_REQUIRED",
                "A completed competitor discovery is required before analysis.",
            )
        return AnalysisDiscoveryLink(
            discovery_id=discovery_id,
            candidate_digest=DIGEST,
            market_snapshot_id=MARKET_ID,
            market_snapshot_checksum=DIGEST,
        )


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def prospect_analyze_payload() -> dict:
    report = json.loads((CONTRACT_DIR / "fixtures" / "prospect.json").read_text(encoding="utf-8"))
    competitors = report["competitor_analysis"]["competitors"]
    return {
        "case_id": report["identity"]["case_id"],
        "report_type": "prospect",
        "business_identity": report["identity"]["business"],
        "primary_service": report["case_context"]["primary_service"],
        "target_market": report["case_context"]["target_market"],
        "queries": report["case_context"]["queries"],
        "competitors": [
            {
                "competitor_id": competitor["competitor_id"],
                "business_name": competitor["business_name"],
                "website_url": competitor["website_url"],
                "public_gbp_url": competitor["public_gbp_url"],
                "confirmation_source": "user",
            }
            for competitor in competitors
        ],
        "first_party_snapshots": [],
        "parent_report": None,
        "generation_limits": {
            "max_site_urls": 500,
            "max_deep_pages": 50,
            "max_competitor_pages_each": 10,
            "competitor_count": 3,
            "max_pagespeed_pages": 5,
            "max_review_samples_each": 30,
        },
    }


def build_app(monkeypatch: pytest.MonkeyPatch, *, enabled: bool = True):
    monkeypatch.setattr(settings, "V22_ANALYZE_ENABLED", enabled)
    monkeypatch.setattr(settings, "V22_INTERNAL_API_TOKEN", "test-internal-token")
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    store = DurableJobStore(redis, prefix="test:v22", state_ttl_seconds=604800)
    queue = RecordingQueue()
    runtime = V22JobRuntime(
        store=store,
        queue=queue,
        redis=redis,
        discovery_verifier=RecordingDiscoveryVerifier(),
    )
    app = create_app()
    app.state.v22_runtime = runtime
    return app, runtime, store, queue


@pytest.mark.anyio
async def test_feature_flag_blocks_analyze_before_redis_write(monkeypatch: pytest.MonkeyPatch) -> None:
    app, _, store, queue = build_app(monkeypatch, enabled=False)
    app.state.v22_runtime = None
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/v2/analyze", headers=AUTH_HEADERS, json=prospect_analyze_payload())

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "V22_ANALYSIS_NOT_READY"
    assert await store.get_state(JOB_ID) is None
    assert queue.calls == []


@pytest.mark.anyio
async def test_submit_get_and_idempotent_replay(monkeypatch: pytest.MonkeyPatch) -> None:
    app, _, _, queue = build_app(monkeypatch)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post("/api/v2/analyze", headers=AUTH_HEADERS, json=prospect_analyze_payload())
        duplicate = await client.post("/api/v2/analyze", headers=AUTH_HEADERS, json=prospect_analyze_payload())
        status = await client.get(f"/api/v2/tasks/{JOB_ID}", headers=AUTH_HEADERS)

    assert first.status_code == 202
    assert duplicate.status_code == 202
    assert first.json() == duplicate.json()
    assert first.json()["job_id"] == str(JOB_ID)
    assert status.status_code == 200
    assert status.json()["status"] == "queued"
    assert queue.calls == [(JOB_ID, 1)]


@pytest.mark.anyio
async def test_idempotency_conflict_returns_409(monkeypatch: pytest.MonkeyPatch) -> None:
    app, _, _, _ = build_app(monkeypatch)
    transport = httpx.ASGITransport(app=app)
    changed = prospect_analyze_payload()
    changed["queries"] = ["different query", *changed["queries"][1:]]

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post("/api/v2/analyze", headers=AUTH_HEADERS, json=prospect_analyze_payload())
        response = await client.post("/api/v2/analyze", headers=AUTH_HEADERS, json=changed)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "IDEMPOTENCY_CONFLICT"


@pytest.mark.anyio
async def test_missing_discovery_link_is_rejected_before_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    app, _, store, queue = build_app(monkeypatch)
    transport = httpx.ASGITransport(app=app)
    headers = {key: value for key, value in AUTH_HEADERS.items() if key != "X-SearchTrust-Discovery-ID"}

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/v2/analyze", headers=headers, json=prospect_analyze_payload())

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "COMPETITOR_DISCOVERY_REQUIRED"
    assert await store.get_state(JOB_ID) is None
    assert queue.calls == []


@pytest.mark.anyio
async def test_replay_cannot_switch_discovery_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    app, _, _, queue = build_app(monkeypatch)
    transport = httpx.ASGITransport(app=app)
    switched = {
        **AUTH_HEADERS,
        "X-SearchTrust-Discovery-ID": "99999999-9999-4999-8999-999999999999",
    }

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post("/api/v2/analyze", headers=AUTH_HEADERS, json=prospect_analyze_payload())
        response = await client.post(
            "/api/v2/analyze", headers=switched, json=prospect_analyze_payload()
        )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert queue.calls == [(JOB_ID, 1)]


@pytest.mark.anyio
async def test_manual_retry_requires_a_new_paid_logical_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    app, _, store, queue = build_app(monkeypatch)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post("/api/v2/analyze", headers=AUTH_HEADERS, json=prospect_analyze_payload())
        now = utc_now()
        await store.transition(
            JOB_ID,
            status="running",
            stage="collecting_site",
            progress=5,
            message="Running",
            now=now,
            attempt_count=1,
        )
        error = JobErrorState(
            error_code="JOB_RETRY_EXHAUSTED",
            user_message="Please retry later.",
            retryable=True,
            stage="failed",
            diagnostic_id=UUID("77777777-7777-4777-8777-777777777777"),
        )
        await store.transition(
            JOB_ID,
            status="failed",
            stage="failed",
            progress=5,
            message=error.user_message,
            now=now + timedelta(seconds=1),
            error=error,
        )
        response = await client.post(f"/api/v2/tasks/{JOB_ID}/retry", headers=AUTH_HEADERS)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "JOB_NEW_ATTEMPT_REQUIRED"
    assert queue.calls == [(JOB_ID, 1)]


@pytest.mark.anyio
async def test_missing_runtime_returns_queue_unavailable_but_v1_stays_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "V22_ANALYZE_ENABLED", True)
    monkeypatch.setattr(settings, "V22_INTERNAL_API_TOKEN", "test-internal-token")
    app = create_app()
    app.state.v22_runtime = None
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        unavailable = await client.post(
            "/api/v2/analyze", headers=AUTH_HEADERS, json=prospect_analyze_payload()
        )
        health = await client.get("/api/v1/health")

    assert unavailable.status_code == 503
    assert unavailable.json()["detail"]["code"] == "QUEUE_UNAVAILABLE"
    assert health.status_code == 200


def verified_task_payload() -> dict:
    return {
        "schema_version": "v22_verified_task_request_v1",
        "case_id": str(CASE_ID),
        "parent_report_id": "22222222-2222-4222-8222-222222222222",
        "gsc_snapshot_id": "33333333-3333-4333-8333-333333333333",
        "ga4_snapshot_id": "44444444-4444-4444-8444-444444444444",
        "public_gbp_snapshot_id": "66666666-6666-4666-8666-666666666666",
        "input_checksum": DIGEST,
    }


def build_verified_app(monkeypatch: pytest.MonkeyPatch, *, enabled: bool = True):
    monkeypatch.setattr(settings, "V22_VERIFIED_ANALYSIS_ENABLED", enabled)
    monkeypatch.setattr(settings, "V22_CALLBACK_URL", "https://app.example.com/callback")
    monkeypatch.setattr(settings, "V22_CALLBACK_SECRET", SecretStr("callback-secret"))
    app, runtime, store, queue = build_app(monkeypatch, enabled=False)

    class ForbiddenDiscoveryVerifier:
        async def verify(self, **kwargs):
            pytest.fail("Verified jobs must never invoke competitor discovery verification")

    runtime.discovery_verifier = ForbiddenDiscoveryVerifier()
    return app, runtime, store, queue


@pytest.mark.anyio
async def test_verified_feature_flag_blocks_before_redis_write(monkeypatch: pytest.MonkeyPatch) -> None:
    app, _, store, queue = build_verified_app(monkeypatch, enabled=False)
    monkeypatch.setattr(settings, "V22_ANALYZE_ENABLED", True)
    app.state.v22_runtime = None
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/v2/verified-analyze", headers=VERIFIED_HEADERS, json=verified_task_payload())

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "V22_VERIFIED_ANALYSIS_NOT_READY"
    assert await store.get_state(VERIFIED_JOB_ID) is None
    assert queue.calls == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("callback_url", "callback_secret"),
    [("", "callback-secret"), ("https://app.example.com/callback", ""), ("", "")],
)
async def test_verified_incomplete_callback_blocks_before_redis_write(
    monkeypatch: pytest.MonkeyPatch, callback_url: str, callback_secret: str
) -> None:
    app, _, store, queue = build_verified_app(monkeypatch)
    monkeypatch.setattr(settings, "V22_CALLBACK_URL", callback_url)
    monkeypatch.setattr(settings, "V22_CALLBACK_SECRET", SecretStr(callback_secret))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/v2/verified-analyze",
            headers=VERIFIED_HEADERS,
            json=verified_task_payload(),
        )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "V22_VERIFIED_ANALYSIS_NOT_READY"
    assert await store.get_state(VERIFIED_JOB_ID) is None
    assert queue.calls == []


@pytest.mark.anyio
async def test_verified_readiness_uses_the_same_acceptance_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _, _, _ = build_verified_app(monkeypatch)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        ready = await client.head(
            "/api/v2/verified-analyze",
            headers={"Authorization": VERIFIED_HEADERS["Authorization"]},
        )
    assert ready.status_code == 204


@pytest.mark.anyio
async def test_verified_submit_persists_exact_envelope_and_replays_without_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    app, _, store, queue = build_verified_app(monkeypatch)
    payload = verified_task_payload()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        first = await client.post("/api/v2/verified-analyze", headers=VERIFIED_HEADERS, json=payload)
        replay = await client.post("/api/v2/verified-analyze", headers=VERIFIED_HEADERS, json=payload)
        status = await client.get(f"/api/v2/tasks/{VERIFIED_JOB_ID}", headers=VERIFIED_HEADERS)

    assert first.status_code == replay.status_code == 202
    assert first.json() == replay.json() == {"job_id": str(VERIFIED_JOB_ID), "status": "queued", "estimated_seconds": 600}
    assert await store.get_request(VERIFIED_JOB_ID) == {
        "schema_version": "v22_verified_request_envelope_v1",
        "verified_request": payload,
    }
    assert status.status_code == 200
    assert status.json()["status"] == "queued"
    assert status.json()["run_generation"] == 1
    assert queue.calls == [(VERIFIED_JOB_ID, 1)]


@pytest.mark.anyio
@pytest.mark.parametrize("authorization", [None, "Bearer wrong-token", "Basic test-internal-token"])
async def test_verified_submit_requires_internal_auth(monkeypatch: pytest.MonkeyPatch, authorization) -> None:
    app, _, store, queue = build_verified_app(monkeypatch)
    headers = {key: value for key, value in VERIFIED_HEADERS.items() if key != "Authorization"}
    if authorization is not None:
        headers["Authorization"] = authorization
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/v2/verified-analyze", headers=headers, json=verified_task_payload())
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "INTERNAL_AUTH_FAILED"
    assert await store.get_state(VERIFIED_JOB_ID) is None
    assert queue.calls == []


@pytest.mark.anyio
@pytest.mark.parametrize("field,value", [
    ("X-SearchTrust-Job-ID", None), ("X-SearchTrust-Job-ID", "invalid"),
    ("Idempotency-Key", None), ("Idempotency-Key", "short"), ("Idempotency-Key", "a" * 201),
    ("Idempotency-Key", "invalid key"),
])
async def test_verified_submit_rejects_invalid_headers(monkeypatch: pytest.MonkeyPatch, field, value) -> None:
    app, _, store, queue = build_verified_app(monkeypatch)
    headers = dict(VERIFIED_HEADERS)
    if value is None:
        headers.pop(field)
    else:
        headers[field] = value
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/v2/verified-analyze", headers=headers, json=verified_task_payload())
    assert response.status_code == 422
    assert await store.get_state(VERIFIED_JOB_ID) is None
    assert queue.calls == []


@pytest.mark.anyio
@pytest.mark.parametrize("changes", [
    {"input_checksum": "invalid"}, {"schema_version": "wrong"}, {"access_token": "must-not-be-echoed"},
    {"gsc_snapshot_id": "invalid"}, {"ga4_snapshot_id": "33333333-3333-4333-8333-333333333333"},
])
async def test_verified_submit_rejects_invalid_body_safely(monkeypatch: pytest.MonkeyPatch, changes) -> None:
    app, _, store, queue = build_verified_app(monkeypatch)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/v2/verified-analyze", headers=VERIFIED_HEADERS, json={**verified_task_payload(), **changes})
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "VALIDATION_ERROR"
    assert "must-not-be-echoed" not in response.text
    assert await store.get_state(VERIFIED_JOB_ID) is None
    assert queue.calls == []


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["payload", "job_id", "idempotency_key"])
async def test_verified_submit_preserves_durable_identity_conflicts(monkeypatch: pytest.MonkeyPatch, change) -> None:
    app, _, store, queue = build_verified_app(monkeypatch)
    payload = verified_task_payload()
    changed_payload = dict(payload)
    headers = dict(VERIFIED_HEADERS)
    if change == "payload":
        changed_payload["input_checksum"] = "sha256:" + "b" * 64
    elif change == "job_id":
        headers["X-SearchTrust-Job-ID"] = str(JOB_ID)
    else:
        headers["Idempotency-Key"] = "another-verified-intent"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        first = await client.post("/api/v2/verified-analyze", headers=VERIFIED_HEADERS, json=payload)
        conflict = await client.post("/api/v2/verified-analyze", headers=headers, json=changed_payload)
    assert first.status_code == 202
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == ("IDEMPOTENCY_CONFLICT" if change == "payload" else "JOB_IDENTITY_CONFLICT")
    assert (await store.get_request(VERIFIED_JOB_ID))["verified_request"] == payload
    assert queue.calls == [(VERIFIED_JOB_ID, 1)]


@pytest.mark.anyio
async def test_verified_submit_missing_runtime_returns_safe_503(monkeypatch: pytest.MonkeyPatch) -> None:
    app, _, _, _ = build_verified_app(monkeypatch)
    app.state.v22_runtime = None
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/v2/verified-analyze", headers=VERIFIED_HEADERS, json=verified_task_payload())
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "QUEUE_UNAVAILABLE"


@pytest.mark.anyio
@pytest.mark.parametrize("boundary", ["store", "queue"])
@pytest.mark.parametrize("exception_type", [RedisError, OSError])
async def test_verified_submit_preserves_store_and_queue_failure_semantics(monkeypatch: pytest.MonkeyPatch, boundary, exception_type) -> None:
    app, _, store, queue = build_verified_app(monkeypatch)

    async def unavailable(*args, **kwargs):
        raise exception_type("private-infrastructure-detail")

    def unavailable_pipeline(**kwargs):
        raise exception_type("private-infrastructure-detail")

    with monkeypatch.context() as failure:
        if boundary == "store":
            failure.setattr(store.redis, "pipeline", unavailable_pipeline)
        else:
            failure.setattr(queue, "enqueue", unavailable)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/api/v2/verified-analyze", headers=VERIFIED_HEADERS, json=verified_task_payload())

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "QUEUE_UNAVAILABLE"
    assert "private-infrastructure-detail" not in response.text
    state = await store.get_state(VERIFIED_JOB_ID)
    if boundary == "store":
        assert state is None
    else:
        assert state.status == "queued"
        assert state.run_generation == 1
        assert (await store.get_request(VERIFIED_JOB_ID))["verified_request"] == verified_task_payload()
    assert queue.calls == []
