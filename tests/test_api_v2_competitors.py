from __future__ import annotations

from uuid import UUID

import fakeredis.aioredis
import httpx
import pytest

from app.competitors_v22.runtime import CompetitorDiscoveryRuntime
from app.competitors_v22.store import CompetitorDiscoveryStore
from app.core.config import settings
from app.main import create_app
from test_v22_competitor_models import JOB_ID, discovery_request


AUTH_HEADERS = {
    "Authorization": "Bearer test-internal-token",
    "X-SearchTrust-Discovery-Job-ID": str(JOB_ID),
    "Idempotency-Key": "competitor-discovery-1",
}


class RecordingQueue:
    def __init__(self) -> None:
        self.calls: list[tuple[UUID, int]] = []

    async def enqueue(self, discovery_job_id: UUID, run_generation: int) -> bool:
        call = (discovery_job_id, run_generation)
        if call in self.calls:
            return False
        self.calls.append(call)
        return True


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def build_app(monkeypatch: pytest.MonkeyPatch, *, enabled: bool = True):
    monkeypatch.setattr(settings, "V22_COMPETITOR_DISCOVERY_ENABLED", enabled)
    monkeypatch.setattr(settings, "V22_INTERNAL_API_TOKEN", "test-internal-token")
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    store = CompetitorDiscoveryStore(redis, prefix="test:v22", state_ttl_seconds=604_800)
    queue = RecordingQueue()
    runtime = CompetitorDiscoveryRuntime(store=store, queue=queue, redis=redis)
    app = create_app()
    app.state.v22_competitor_runtime = runtime
    return app, store, queue


@pytest.mark.anyio
async def test_competitor_feature_flag_blocks_before_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    app, store, queue = build_app(monkeypatch, enabled=False)
    app.state.v22_competitor_runtime = None
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v2/competitors/discover",
            headers=AUTH_HEADERS,
            json=discovery_request().model_dump(mode="json"),
        )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "V22_COMPETITOR_DISCOVERY_NOT_READY"
    assert await store.get_state(JOB_ID) is None
    assert queue.calls == []


@pytest.mark.anyio
async def test_submit_status_and_idempotent_replay(monkeypatch: pytest.MonkeyPatch) -> None:
    app, _, queue = build_app(monkeypatch)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post(
            "/api/v2/competitors/discover",
            headers=AUTH_HEADERS,
            json=discovery_request().model_dump(mode="json"),
        )
        replay = await client.post(
            "/api/v2/competitors/discover",
            headers=AUTH_HEADERS,
            json=discovery_request().model_dump(mode="json"),
        )
        status = await client.get(
            f"/api/v2/competitors/tasks/{JOB_ID}",
            headers=AUTH_HEADERS,
        )

    assert first.status_code == replay.status_code == 202
    assert first.json() == replay.json()
    assert status.status_code == 200
    assert status.json()["status"] == "queued"
    assert queue.calls == [(JOB_ID, 1)]


@pytest.mark.anyio
async def test_competitor_endpoint_requires_internal_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    app, _, _ = build_app(monkeypatch)
    transport = httpx.ASGITransport(app=app)
    headers = {key: value for key, value in AUTH_HEADERS.items() if key != "Authorization"}

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v2/competitors/discover",
            headers=headers,
            json=discovery_request().model_dump(mode="json"),
        )

    assert response.status_code == 401

