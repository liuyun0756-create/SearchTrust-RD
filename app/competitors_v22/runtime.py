"""Web-process runtime for durable competitor discovery."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from app.api.v2.competitor_models import (
    CompetitorDiscoveryRequest,
    CompetitorDiscoveryRetryResponse,
    CompetitorDiscoveryTaskCreateResponse,
)
from app.competitors_v22.queue import ArqCompetitorDiscoveryQueue, CompetitorDiscoveryQueue
from app.competitors_v22.store import CompetitorDiscoveryStore
from app.core.config import settings


class CompetitorDiscoveryRuntime:
    def __init__(
        self,
        *,
        store: CompetitorDiscoveryStore,
        queue: CompetitorDiscoveryQueue,
        redis: Any,
    ) -> None:
        self.store = store
        self.queue = queue
        self.redis = redis

    async def submit(
        self,
        *,
        discovery_job_id: UUID,
        idempotency_key: str,
        request: CompetitorDiscoveryRequest,
    ) -> CompetitorDiscoveryTaskCreateResponse:
        registered = await self.store.register_job(
            discovery_job_id=discovery_job_id,
            case_id=request.case_id,
            idempotency_key=idempotency_key,
            request_payload=request.model_dump(mode="json"),
            now=datetime.now(timezone.utc),
        )
        if not registered.replayed:
            await self.queue.enqueue(discovery_job_id, registered.state.run_generation)
        return CompetitorDiscoveryTaskCreateResponse(
            discovery_job_id=discovery_job_id,
            status="queued",
            estimated_seconds=120,
        )

    async def retry(self, discovery_job_id: UUID) -> CompetitorDiscoveryRetryResponse:
        state = await self.store.retry_failed(
            discovery_job_id, now=datetime.now(timezone.utc)
        )
        await self.queue.enqueue(discovery_job_id, state.run_generation)
        return CompetitorDiscoveryRetryResponse(
            discovery_job_id=discovery_job_id,
            status="queued",
            attempt_count=state.attempt_count + 1,
        )


def create_competitor_discovery_runtime(redis: Any) -> CompetitorDiscoveryRuntime:
    store = CompetitorDiscoveryStore(
        redis,
        prefix=settings.V22_REDIS_PREFIX,
        state_ttl_seconds=settings.V22_COMPETITOR_STATE_TTL_SECONDS,
    )
    queue = ArqCompetitorDiscoveryQueue(redis, queue_name=settings.V22_QUEUE_NAME)
    return CompetitorDiscoveryRuntime(store=store, queue=queue, redis=redis)
