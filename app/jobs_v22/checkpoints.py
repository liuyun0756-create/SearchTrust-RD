"""Persisted stage checkpoints for at-least-once worker execution."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar
from uuid import UUID

from redis.asyncio import Redis
from redis.exceptions import WatchError

from app.jobs_v22.digest import canonical_json_bytes
from app.jobs_v22.keys import JobRedisKeys


T = TypeVar("T")


class JobCheckpoints:
    def __init__(
        self,
        redis: Redis,
        *,
        prefix: str,
        ttl_seconds: int,
        run_generation: int | None = None,
    ) -> None:
        self.redis = redis
        self.keys = JobRedisKeys(prefix)
        self.ttl_seconds = ttl_seconds
        self.run_generation = run_generation

    async def get(self, job_id: UUID, checkpoint_key: str) -> Any | None:
        raw = await self.redis.get(self.keys.checkpoint(job_id, checkpoint_key))
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        return json.loads(raw)

    async def save(self, job_id: UUID, checkpoint_key: str, value: Any) -> bool:
        checkpoint = self.keys.checkpoint(job_id, checkpoint_key)
        if self.run_generation is None:
            result = await self.redis.set(
                checkpoint,
                canonical_json_bytes(value),
                ex=self.ttl_seconds,
                nx=True,
            )
            return bool(result)

        state_key = self.keys.state(job_id)
        async with self.redis.pipeline(transaction=True) as pipe:
            while True:
                try:
                    await pipe.watch(state_key, checkpoint)
                    raw = await pipe.get(state_key)
                    if raw is None:
                        return False
                    state = json.loads(raw)
                    if state.get("run_generation") != self.run_generation or state.get("status") in {"succeeded", "failed"}:
                        return False
                    if await pipe.exists(checkpoint):
                        return False
                    pipe.multi()
                    pipe.set(checkpoint, canonical_json_bytes(value), ex=self.ttl_seconds)
                    await pipe.execute()
                    return True
                except WatchError:
                    continue

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
