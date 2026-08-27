"""Persisted stage checkpoints for at-least-once worker execution."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar
from uuid import UUID

from redis.asyncio import Redis

from app.jobs_v22.digest import canonical_json_bytes
from app.jobs_v22.keys import JobRedisKeys


T = TypeVar("T")


class JobCheckpoints:
    def __init__(self, redis: Redis, *, prefix: str, ttl_seconds: int) -> None:
        self.redis = redis
        self.keys = JobRedisKeys(prefix)
        self.ttl_seconds = ttl_seconds

    async def get(self, job_id: UUID, checkpoint_key: str) -> Any | None:
        raw = await self.redis.get(self.keys.checkpoint(job_id, checkpoint_key))
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        return json.loads(raw)

    async def save(self, job_id: UUID, checkpoint_key: str, value: Any) -> bool:
        result = await self.redis.set(
            self.keys.checkpoint(job_id, checkpoint_key),
            canonical_json_bytes(value),
            ex=self.ttl_seconds,
            nx=True,
        )
        return bool(result)

    async def run_once(
        self,
        job_id: UUID,
        checkpoint_key: str,
        operation: Callable[[], Awaitable[T]],
    ) -> T:
        existing = await self.get(job_id, checkpoint_key)
        if existing is not None:
            return existing
        result = await operation()
        if await self.save(job_id, checkpoint_key, result):
            return result
        # A concurrent execution committed first. Its persisted value is the
        # canonical checkpoint even if both external calls briefly overlapped.
        persisted = await self.get(job_id, checkpoint_key)
        return result if persisted is None else persisted
