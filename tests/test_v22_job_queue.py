from uuid import UUID

import pytest

from app.jobs_v22.queue import ArqJobQueue, physical_job_id


JOB_ID = UUID("55555555-5555-4555-8555-555555555555")


class RecordingPool:
    def __init__(self) -> None:
        self.job_ids: set[str] = set()
        self.calls: list[dict] = []

    async def enqueue_job(self, function: str, *args, **kwargs):
        self.calls.append({"function": function, "args": args, "kwargs": kwargs})
        job_id = kwargs["_job_id"]
        if job_id in self.job_ids:
            return None
        self.job_ids.add(job_id)
        return object()


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_physical_job_id_includes_generation() -> None:
    assert physical_job_id(JOB_ID, 3) == f"v22:{JOB_ID}:run:3"


@pytest.mark.anyio
async def test_duplicate_physical_enqueue_is_suppressed() -> None:
    pool = RecordingPool()
    queue = ArqJobQueue(pool, queue_name="test:v22:queue", expires_seconds=86400)

    first = await queue.enqueue(JOB_ID, 1)
    duplicate = await queue.enqueue(JOB_ID, 1)
    next_generation = await queue.enqueue(JOB_ID, 2)

    assert first is True
    assert duplicate is False
    assert next_generation is True
    assert pool.calls[0]["function"] == "execute_v22_job"
    assert pool.calls[0]["kwargs"]["_queue_name"] == "test:v22:queue"

