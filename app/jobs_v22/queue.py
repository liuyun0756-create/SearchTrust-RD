"""ARQ adapter kept separate from logical durable job state."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from arq.connections import ArqRedis


def physical_job_id(job_id: UUID, run_generation: int) -> str:
    if run_generation < 1:
        raise ValueError("run generation must be positive")
    return f"v22:{job_id}:run:{run_generation}"


class JobQueue(Protocol):
    async def enqueue(self, job_id: UUID, run_generation: int) -> bool: ...


class ArqJobQueue:
    """Enqueue physical executions while ARQ enforces physical ID uniqueness."""

    def __init__(self, pool: ArqRedis, *, queue_name: str, expires_seconds: int = 86400) -> None:
        self.pool = pool
        self.queue_name = queue_name
        self.expires_seconds = expires_seconds

    async def enqueue(self, job_id: UUID, run_generation: int) -> bool:
        job = await self.pool.enqueue_job(
            "execute_v22_job",
            str(job_id),
            run_generation,
            _job_id=physical_job_id(job_id, run_generation),
            _queue_name=self.queue_name,
            _expires=self.expires_seconds,
        )
        return job is not None
