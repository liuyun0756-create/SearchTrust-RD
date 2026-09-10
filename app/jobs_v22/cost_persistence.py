"""Service-only durable persistence and retry outbox for cost summaries."""

from __future__ import annotations

import logging
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from pydantic import ValidationError

from app.jobs_v22.cost_models import CostSummaryRecord
from app.jobs_v22.digest import canonical_json_bytes
from app.jobs_v22.keys import JobRedisKeys


logger = logging.getLogger(__name__)


class CostSummaryPersister:
    def __init__(
        self,
        *,
        url: str,
        service_role_key: str,
        http_client: httpx.AsyncClient,
    ) -> None:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in ("", "/")
            or not service_role_key
        ):
            raise ValueError("V22 cost summary storage is not configured")
        self.url = url.rstrip("/")
        self.service_role_key = service_role_key
        self.http_client = http_client

    async def persist(self, summary: CostSummaryRecord) -> bool:
        body = {
            "p_job_id": str(summary.job_id),
            "p_case_id": str(summary.case_id),
            "p_job_kind": summary.job_kind,
            "p_status": summary.status,
            "p_attempt_count": summary.attempt_count,
            "p_ledger_revision": summary.ledger_revision,
            "p_cost_counters": summary.cost_counters.root,
            "p_started_at": summary.started_at.isoformat(),
            "p_completed_at": summary.completed_at.isoformat(),
        }
        try:
            response = await self.http_client.post(
                f"{self.url}/rest/v1/rpc/upsert_v22_job_cost_summary",
                headers={
                    "apikey": self.service_role_key,
                    "authorization": f"Bearer {self.service_role_key}",
                    "content-type": "application/json",
                },
                content=canonical_json_bytes(body),
            )
        except httpx.HTTPError:
            return False
        return 200 <= response.status_code < 300


class CostSummaryOutbox:
    def __init__(
        self,
        redis,
        *,
        prefix: str,
        ttl_seconds: int,
        persister: CostSummaryPersister,
    ) -> None:
        if ttl_seconds < 3600:
            raise ValueError("cost summary outbox TTL is too short")
        self.redis = redis
        self.keys = JobRedisKeys(prefix)
        self.ttl_seconds = ttl_seconds
        self.persister = persister

    async def enqueue(self, summary: CostSummaryRecord) -> None:
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.set(
                self.keys.cost_summary(summary.job_id),
                summary.model_dump_json(),
                ex=self.ttl_seconds,
            )
            pipe.zadd(
                self.keys.cost_sync_pending,
                {str(summary.job_id): summary.completed_at.timestamp()},
            )
            await pipe.execute()

    async def sync(self, job_id: UUID) -> bool:
        raw = await self.redis.get(self.keys.cost_summary(job_id))
        if raw is None:
            await self.redis.zrem(self.keys.cost_sync_pending, str(job_id))
            return True
        try:
            summary = CostSummaryRecord.model_validate_json(raw)
        except (TypeError, ValueError, ValidationError):
            logger.warning("cost summary rejected job_id_suffix=%s", str(job_id)[-8:])
            return False
        if summary.job_id != job_id or not await self.persister.persist(summary):
            return False
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.zrem(self.keys.cost_sync_pending, str(job_id))
            pipe.delete(self.keys.cost_summary(job_id))
            await pipe.execute()
        return True

    async def flush(self, *, limit: int = 50) -> int:
        if not 1 <= limit <= 500:
            raise ValueError("cost summary flush limit is invalid")
        raw_ids = await self.redis.zrange(self.keys.cost_sync_pending, 0, limit - 1)
        synced = 0
        for raw_id in raw_ids:
            try:
                value = raw_id.decode("utf-8") if isinstance(raw_id, bytes) else str(raw_id)
                job_id = UUID(value)
            except (UnicodeError, ValueError):
                await self.redis.zrem(self.keys.cost_sync_pending, raw_id)
                continue
            if await self.sync(job_id):
                synced += 1
        return synced
