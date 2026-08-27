"""Redis-backed source of truth for logical v2.2 task state."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping
from uuid import UUID

from redis.asyncio import Redis
from redis.exceptions import WatchError

from app.jobs_v22.digest import canonical_json_bytes, request_digest
from app.jobs_v22.errors import (
    IdempotencyConflict,
    InvalidJobTransition,
    JobIdentityConflict,
    JobNotFound,
)
from app.jobs_v22.keys import JobRedisKeys
from app.jobs_v22.models import JobErrorState, JobState, JobStatus
from app.report_v22.models import ReportV22


@dataclass(frozen=True)
class RegistrationResult:
    state: JobState
    replayed: bool


@dataclass(frozen=True)
class TransitionResult:
    state: JobState
    applied: bool


_ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "queued": {"queued", "running", "failed"},
    "running": {"running", "queued", "succeeded", "failed"},
    "succeeded": set(),
    "failed": set(),
}


def _decode_json(raw: bytes | str) -> Any:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    return json.loads(raw)


class DurableJobStore:
    """Atomic durable state operations shared by Web and Worker processes."""

    def __init__(self, redis: Redis, *, prefix: str, state_ttl_seconds: int) -> None:
        self.redis = redis
        self.keys = JobRedisKeys(prefix)
        self.state_ttl_seconds = state_ttl_seconds

    async def register_job(
        self,
        *,
        job_id: UUID,
        case_id: UUID,
        idempotency_key: str,
        request_payload: Mapping[str, Any],
        now: datetime,
    ) -> RegistrationResult:
        digest = request_digest(request_payload)
        idem_key = self.keys.idempotency(case_id, idempotency_key)
        state_key = self.keys.state(job_id)
        request_key = self.keys.request(job_id)
        initial = JobState(
            job_id=job_id,
            case_id=case_id,
            status="queued",
            stage="queued",
            progress=0,
            message="Queued. Analysis will begin shortly.",
            attempt_count=0,
            run_generation=1,
            revision=1,
            request_digest=digest,
            heartbeat_at=None,
            created_at=now,
            updated_at=now,
            completed_at=None,
            report=None,
            error=None,
            cost_counters={},
            callback_synced_revision=0,
        )
        identity = {
            "job_id": str(job_id),
            "case_id": str(case_id),
            "request_digest": digest,
        }

        async with self.redis.pipeline(transaction=True) as pipe:
            while True:
                try:
                    await pipe.watch(idem_key, state_key)
                    existing_identity_raw = await pipe.get(idem_key)
                    if existing_identity_raw is not None:
                        existing_identity = _decode_json(existing_identity_raw)
                        if existing_identity.get("request_digest") != digest:
                            raise IdempotencyConflict()
                        if existing_identity.get("job_id") != str(job_id):
                            raise JobIdentityConflict()
                        existing = await self.get_state(job_id, client=pipe)
                        if existing is None:
                            raise JobIdentityConflict()
                        return RegistrationResult(existing, replayed=True)

                    existing_state = await self.get_state(job_id, client=pipe)
                    if existing_state is not None:
                        if existing_state.case_id != case_id or existing_state.request_digest != digest:
                            raise JobIdentityConflict()
                        return RegistrationResult(existing_state, replayed=True)

                    pipe.multi()
                    pipe.set(idem_key, canonical_json_bytes(identity), ex=self.state_ttl_seconds)
                    pipe.set(request_key, canonical_json_bytes(request_payload), ex=self.state_ttl_seconds)
                    pipe.set(state_key, initial.model_dump_json(), ex=self.state_ttl_seconds)
                    pipe.zadd(self.keys.active, {str(job_id): now.timestamp()})
                    pipe.zadd(self.keys.sync_pending, {str(job_id): initial.revision})
                    await pipe.execute()
                    break
                except WatchError:
                    continue

        await self.redis.publish(self.keys.events(job_id), str(initial.revision))
        return RegistrationResult(initial, replayed=False)

    async def get_state(self, job_id: UUID, *, client: Any | None = None) -> JobState | None:
        source = client or self.redis
        raw = await source.get(self.keys.state(job_id))
        if raw is None:
            return None
        return JobState.model_validate_json(raw)

    async def require_state(self, job_id: UUID) -> JobState:
        state = await self.get_state(job_id)
        if state is None:
            raise JobNotFound()
        return state

    async def get_request(self, job_id: UUID) -> dict[str, Any] | None:
        raw = await self.redis.get(self.keys.request(job_id))
        if raw is None:
            return None
        value = _decode_json(raw)
        return value if isinstance(value, dict) else None

    async def transition(
        self,
        job_id: UUID,
        *,
        status: JobStatus,
        stage: str,
        progress: int,
        message: str,
        now: datetime,
        attempt_count: int | None = None,
        heartbeat_at: datetime | None = None,
        report: ReportV22 | None = None,
        error: JobErrorState | None = None,
        cost_counters: dict[str, int | float] | None = None,
    ) -> TransitionResult:
        state_key = self.keys.state(job_id)
        next_state: JobState | None = None

        async with self.redis.pipeline(transaction=True) as pipe:
            while True:
                try:
                    await pipe.watch(state_key)
                    current = await self.get_state(job_id, client=pipe)
                    if current is None:
                        raise JobNotFound()
                    if current.terminal:
                        return TransitionResult(current, applied=False)
                    if status not in _ALLOWED_TRANSITIONS[current.status]:
                        raise InvalidJobTransition(
                            f"Transition from {current.status} to {status} is not allowed."
                        )

                    completed_at = now if status in {"succeeded", "failed"} else None
                    next_state = current.model_copy(
                        update={
                            "status": status,
                            "stage": stage,
                            "progress": progress,
                            "message": message,
                            "attempt_count": (
                                current.attempt_count if attempt_count is None else attempt_count
                            ),
                            "revision": current.revision + 1,
                            "heartbeat_at": (
                                heartbeat_at
                                if heartbeat_at is not None
                                else (now if status == "running" else current.heartbeat_at)
                            ),
                            "updated_at": now,
                            "completed_at": completed_at,
                            "report": report,
                            "error": error,
                            "cost_counters": (
                                current.cost_counters if cost_counters is None else cost_counters
                            ),
                        }
                    )
                    next_state = JobState.model_validate(next_state.model_dump())

                    pipe.multi()
                    pipe.set(state_key, next_state.model_dump_json(), ex=self.state_ttl_seconds)
                    pipe.expire(self.keys.request(job_id), self.state_ttl_seconds)
                    if next_state.terminal:
                        pipe.zrem(self.keys.active, str(job_id))
                    else:
                        pipe.zadd(self.keys.active, {str(job_id): now.timestamp()})
                    pipe.zadd(self.keys.sync_pending, {str(job_id): next_state.revision})
                    await pipe.execute()
                    break
                except WatchError:
                    continue

        assert next_state is not None
        await self.redis.publish(self.keys.events(job_id), str(next_state.revision))
        return TransitionResult(next_state, applied=True)

    async def mark_callback_synced(self, job_id: UUID, revision: int) -> JobState:
        state_key = self.keys.state(job_id)
        next_state: JobState | None = None
        async with self.redis.pipeline(transaction=True) as pipe:
            while True:
                try:
                    await pipe.watch(state_key)
                    current = await self.get_state(job_id, client=pipe)
                    if current is None:
                        raise JobNotFound()
                    synced = min(max(revision, current.callback_synced_revision), current.revision)
                    next_state = current.model_copy(update={"callback_synced_revision": synced})
                    pipe.multi()
                    pipe.set(state_key, next_state.model_dump_json(), ex=self.state_ttl_seconds)
                    if synced >= current.revision:
                        pipe.zrem(self.keys.sync_pending, str(job_id))
                    await pipe.execute()
                    break
                except WatchError:
                    continue
        assert next_state is not None
        return next_state

    async def active_count(self) -> int:
        return int(await self.redis.zcard(self.keys.active))

    async def pending_callback_count(self) -> int:
        return int(await self.redis.zcard(self.keys.sync_pending))

    async def list_stale_jobs(self, cutoff: datetime, *, limit: int = 100) -> list[UUID]:
        values = await self.redis.zrangebyscore(
            self.keys.active,
            min="-inf",
            max=cutoff.timestamp(),
            start=0,
            num=limit,
        )
        return [UUID(value.decode() if isinstance(value, bytes) else value) for value in values]

    async def list_pending_callbacks(self, *, limit: int = 100) -> list[UUID]:
        values = await self.redis.zrange(self.keys.sync_pending, 0, limit - 1)
        return [UUID(value.decode() if isinstance(value, bytes) else value) for value in values]
