from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from uuid import UUID

import fakeredis.aioredis
import pytest
from hypothesis import given, settings, strategies as st

from app.jobs_v22.callbacks import CallbackSynchronizer
from app.jobs_v22.errors import IdempotencyConflict
from app.jobs_v22.models import JobErrorState
from app.jobs_v22.store import DurableJobStore


JOB_ID = UUID("55555555-5555-4555-8555-555555555555")
CASE_ID = UUID("11111111-1111-4111-8111-111111111111")
NOW = datetime(2026, 9, 11, 8, 0, tzinfo=timezone.utc)


def _store() -> DurableJobStore:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    return DurableJobStore(redis, prefix="property:v22", state_ttl_seconds=3_600)


async def _register(store: DurableJobStore, payload: dict) -> object:
    return await store.register_job(
        job_id=JOB_ID,
        case_id=CASE_ID,
        idempotency_key="synthetic-intent",
        request_payload=payload,
        now=NOW,
    )


@pytest.mark.anyio
@given(st.integers(min_value=1, max_value=20))
@settings(max_examples=20)
async def test_duplicate_registration_sequences_create_one_logical_job(count: int) -> None:
    store = _store()
    payload = {"case_id": str(CASE_ID), "queries": ["a", "b", "c"]}
    results = await asyncio.gather(*(_register(store, payload) for _ in range(count)))

    assert sum(not result.replayed for result in results) == 1
    assert {result.state.revision for result in results} == {1}
    assert await store.active_count() == 1


@pytest.mark.anyio
@given(st.lists(st.booleans(), min_size=1, max_size=12))
@settings(max_examples=20)
async def test_conflicting_replays_never_replace_original_identity(conflicts) -> None:
    store = _store()
    original = {"case_id": str(CASE_ID), "queries": ["a", "b", "c"]}
    await _register(store, original)

    for conflict in conflicts:
        payload = (
            {**original, "queries": ["different", "b", "c"]}
            if conflict
            else original
        )
        if conflict:
            with pytest.raises(IdempotencyConflict):
                await _register(store, payload)
        else:
            assert (await _register(store, payload)).replayed is True

    assert await store.get_request(JOB_ID) == original
    assert (await store.require_state(JOB_ID)).revision == 1


@pytest.mark.anyio
@given(st.lists(st.booleans(), min_size=0, max_size=8))
@settings(max_examples=20)
async def test_callback_success_settles_once_after_any_failure_prefix(failures) -> None:
    store = _store()
    await _register(store, {"case_id": str(CASE_ID)})
    error = JobErrorState(
        error_code="V22_PROVIDER_UNAVAILABLE",
        user_message="The synthetic task stopped safely.",
        retryable=True,
        stage="failed",
        diagnostic_id=UUID("77777777-7777-4777-8777-777777777777"),
    )
    terminal = await store.transition(
        JOB_ID,
        status="failed",
        stage="failed",
        progress=20,
        message=error.user_message,
        now=NOW + timedelta(seconds=1),
        error=error,
    )
    responses = [False for _ in failures] + [True]

    class Sender:
        def __init__(self) -> None:
            self.calls = 0
            self.revisions: list[int] = []

        async def send(self, state) -> bool:
            assert state.status == "failed"
            self.revisions.append(state.revision)
            result = responses[self.calls]
            self.calls += 1
            return result

    sender = Sender()
    synchronizer = CallbackSynchronizer(store, sender)
    outcomes = [await synchronizer.sync(JOB_ID) for _ in responses]
    outcomes.extend([await synchronizer.sync(JOB_ID) for _ in range(3)])

    assert outcomes[-4:] == [True, True, True, True]
    assert sender.calls == len(responses)
    assert sender.revisions == [terminal.state.revision] * len(responses)
    assert (
        await store.require_state(JOB_ID)
    ).callback_synced_revision == terminal.state.revision
    assert await store.pending_callback_count() == 0
