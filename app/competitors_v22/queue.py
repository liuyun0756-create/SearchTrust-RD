"""ARQ adapter for competitor discovery physical executions."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from arq.connections import ArqRedis


def competitor_discovery_physical_job_id(discovery_job_id: UUID, run_generation: int) -> str:
    if run_generation < 1:
        raise ValueError("run generation must be positive")
    return f"v22:competitor-discovery:{discovery_job_id}:run:{run_generation}"


class CompetitorDiscoveryQueue(Protocol):
    async def enqueue(self, discovery_job_id: UUID, run_generation: int) -> bool: ...


class ArqCompetitorDiscoveryQueue:
    def __init__(self, pool: ArqRedis, *, queue_name: str, expires_seconds: int = 86400) -> None:
        self.pool = pool
        self.queue_name = queue_name
        self.expires_seconds = expires_seconds

    async def enqueue(self, discovery_job_id: UUID, run_generation: int) -> bool:
        job = await self.pool.enqueue_job(
            "execute_v22_competitor_discovery",
            str(discovery_job_id),
            run_generation,
            _job_id=competitor_discovery_physical_job_id(discovery_job_id, run_generation),
            _queue_name=self.queue_name,
            _expires=self.expires_seconds,
        )
        return job is not None
