from datetime import timedelta
from uuid import UUID

import fakeredis.aioredis
import pytest

from app.api.v2.runtime import SSE_HEARTBEAT, stream_job_states
from app.jobs_v22.models import JobErrorState, utc_now
from app.jobs_v22.store import DurableJobStore


JOB_ID = UUID("55555555-5555-4555-8555-555555555555")
CASE_ID = UUID("11111111-1111-4111-8111-111111111111")


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
        now=utc_now(),
    )
    return store


@pytest.mark.anyio
async def test_stream_sends_current_snapshot_then_heartbeat() -> None:
    store = await build_store()
    stream = stream_job_states(store, JOB_ID, heartbeat_interval=0.01, timeout=0.1)
    try:
        first = await anext(stream)
        heartbeat = await anext(stream)
    finally:
        await stream.aclose()

    assert first.revision == 1
    assert heartbeat is SSE_HEARTBEAT


@pytest.mark.anyio
async def test_reconnect_skips_old_revision_and_recovers_latest_state() -> None:
    store = await build_store()
    now = utc_now()
    await store.transition(
        JOB_ID,
        status="running",
        stage="collecting_site",
        progress=10,
        message="Running",
        now=now,
        attempt_count=1,
    )
    stream = stream_job_states(
        store,
        JOB_ID,
        last_event_id=1,
        heartbeat_interval=0.01,
        timeout=0.1,
    )
    try:
        recovered = await anext(stream)
    finally:
        await stream.aclose()

    assert recovered.revision == 2
    assert recovered.status == "running"


@pytest.mark.anyio
async def test_terminal_snapshot_closes_stream() -> None:
    store = await build_store()
    now = utc_now()
    error = JobErrorState(
        error_code="V22_INPUT_INVALID",
        user_message="The request is invalid.",
        retryable=False,
        stage="failed",
        diagnostic_id=UUID("77777777-7777-4777-8777-777777777777"),
    )
    await store.transition(
        JOB_ID,
        status="failed",
        stage="failed",
        progress=0,
        message=error.user_message,
        now=now + timedelta(seconds=1),
        error=error,
    )
    stream = stream_job_states(store, JOB_ID, heartbeat_interval=0.01, timeout=0.1)

    terminal = await anext(stream)
    with pytest.raises(StopAsyncIteration):
        await anext(stream)

    assert terminal.status == "failed"
