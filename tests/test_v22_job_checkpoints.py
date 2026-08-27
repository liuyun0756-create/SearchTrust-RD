from uuid import UUID

import fakeredis.aioredis
import pytest

from app.jobs_v22.checkpoints import JobCheckpoints


JOB_ID = UUID("55555555-5555-4555-8555-555555555555")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_run_once_reuses_persisted_checkpoint() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    checkpoints = JobCheckpoints(redis, prefix="test:v22", ttl_seconds=604800)
    calls = 0

    async def operation() -> dict[str, int]:
        nonlocal calls
        calls += 1
        return {"provider_calls": calls}

    first = await checkpoints.run_once(JOB_ID, "collect-site", operation)
    second = await checkpoints.run_once(JOB_ID, "collect-site", operation)

    assert first == {"provider_calls": 1}
    assert second == first
    assert calls == 1

