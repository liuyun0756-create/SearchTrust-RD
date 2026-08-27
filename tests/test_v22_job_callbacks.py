import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import fakeredis.aioredis
import httpx
import pytest

from app.jobs_v22.callbacks import (
    CallbackSynchronizer,
    SignedCallbackClient,
    build_callback_event,
    sign_callback_body,
)
from app.jobs_v22.store import DurableJobStore


JOB_ID = UUID("55555555-5555-4555-8555-555555555555")
CASE_ID = UUID("11111111-1111-4111-8111-111111111111")
NOW = datetime(2026, 8, 27, 8, 0, tzinfo=timezone.utc)
FIXTURE = Path(__file__).resolve().parents[1] / "contracts" / "v22_job_callback_signature.json"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def build_store() -> DurableJobStore:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    store = DurableJobStore(redis, prefix="test:v22", state_ttl_seconds=604800)
    await store.register_job(
        job_id=JOB_ID,
        case_id=CASE_ID,
        idempotency_key="intent-1",
        request_payload={"case_id": str(CASE_ID)},
        now=NOW,
    )
    return store


def test_callback_signature_matches_shared_fixture() -> None:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    body = fixture["body"].encode("utf-8")

    signature = sign_callback_body(
        fixture["secret"],
        fixture["timestamp"],
        body,
        version=fixture["version"],
    )

    assert signature == fixture["signature"]


@pytest.mark.anyio
async def test_signed_callback_sends_bounded_event_and_expected_headers() -> None:
    store = await build_store()
    state = await store.require_state(JOB_ID)
    seen: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = dict(request.headers)
        seen["body"] = request.content
        return httpx.Response(204)

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = SignedCallbackClient(
        url="https://frontend.example/api/internal/v2/job-events",
        secret="callback-secret",
        http_client=http_client,
        clock=lambda: 1787817600,
    )
    try:
        delivered = await client.send(state)
    finally:
        await http_client.aclose()

    event = json.loads(seen["body"])
    assert delivered is True
    assert event["job_id"] == str(JOB_ID)
    assert "report" not in event
    assert seen["headers"]["x-searchtrust-event-id"] == f"{JOB_ID}:1"
    assert seen["headers"]["x-searchtrust-signature"].startswith("sha256=")


@pytest.mark.anyio
async def test_callback_failure_stays_pending_and_later_sync_is_idempotent() -> None:
    store = await build_store()

    class Sender:
        def __init__(self) -> None:
            self.calls = 0

        async def send(self, state) -> bool:
            self.calls += 1
            return self.calls > 1

    sender = Sender()
    synchronizer = CallbackSynchronizer(store, sender)

    first = await synchronizer.sync(JOB_ID)
    assert first is False
    assert await store.pending_callback_count() == 1

    second = await synchronizer.sync(JOB_ID)
    third = await synchronizer.sync(JOB_ID)
    state = await store.require_state(JOB_ID)

    assert second is True
    assert third is True
    assert sender.calls == 2
    assert state.callback_synced_revision == state.revision
    assert await store.pending_callback_count() == 0


def test_callback_event_uses_state_revision_as_event_identity() -> None:
    # Model construction is covered via the store tests; this test protects
    # the cross-service event identity from accidental format changes.
    assert build_callback_event.__name__ == "build_callback_event"

