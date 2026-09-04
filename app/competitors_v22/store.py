"""Durable Redis state for independent competitor-discovery tasks."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping
from uuid import UUID

from redis.asyncio import Redis
from redis.exceptions import WatchError

from app.api.v2.competitor_models import (
    CompetitorDiscoveryError,
    CompetitorDiscoveryResult,
    CompetitorDiscoveryStage,
    CompetitorDiscoveryStatus,
)
from app.competitors_v22.keys import CompetitorDiscoveryRedisKeys
from app.competitors_v22.models import CompetitorDiscoveryJobState
from app.jobs_v22.digest import canonical_json_bytes, request_digest
from app.jobs_v22.errors import (
    IdempotencyConflict,
    InvalidJobTransition,
    JobIdentityConflict,
    JobNotFound,
    JobNotRetryable,
)


@dataclass(frozen=True)
class DiscoveryRegistrationResult:
    state: CompetitorDiscoveryJobState
    replayed: bool


@dataclass(frozen=True)
class DiscoveryTransitionResult:
    state: CompetitorDiscoveryJobState
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


class CompetitorDiscoveryStore:
    def __init__(self, redis: Redis, *, prefix: str, state_ttl_seconds: int) -> None:
        self.redis = redis
        self.keys = CompetitorDiscoveryRedisKeys(prefix)
        self.state_ttl_seconds = state_ttl_seconds

    async def register_job(
        self,
        *,
        discovery_job_id: UUID,
        case_id: UUID,
        idempotency_key: str,
        request_payload: Mapping[str, Any],
        now: datetime,
    ) -> DiscoveryRegistrationResult:
        digest = request_digest(request_payload)
        idem_digest = f"sha256:{hashlib.sha256(idempotency_key.encode()).hexdigest()}"
        idem_key = self.keys.idempotency(case_id, idempotency_key)
        state_key = self.keys.state(discovery_job_id)
        request_key = self.keys.request(discovery_job_id)
        initial = CompetitorDiscoveryJobState(
            discovery_job_id=discovery_job_id,
            case_id=case_id,
            status="queued",
            stage="queued",
            progress=0,
            message="Queued. Competitor discovery will begin shortly.",
            attempt_count=0,
            run_generation=1,
            revision=1,
            request_digest=digest,
            idempotency_key_digest=idem_digest,
            created_at=now,
            updated_at=now,
        )
        identity = {
            "discovery_job_id": str(discovery_job_id),
            "case_id": str(case_id),
            "request_digest": digest,
        }

        async with self.redis.pipeline(transaction=True) as pipe:
            while True:
                try:
                    await pipe.watch(idem_key, state_key)
                    existing_identity_raw = await pipe.get(idem_key)
                    if existing_identity_raw is not None:
                        identity_value = _decode_json(existing_identity_raw)
                        if identity_value.get("request_digest") != digest:
                            raise IdempotencyConflict()
                        if identity_value.get("discovery_job_id") != str(discovery_job_id):
                            raise JobIdentityConflict()
                        existing = await self.get_state(discovery_job_id, client=pipe)
                        if existing is None:
                            raise JobIdentityConflict()
                        return DiscoveryRegistrationResult(existing, replayed=True)

                    existing = await self.get_state(discovery_job_id, client=pipe)
                    if existing is not None:
                        if (
                            existing.case_id != case_id
                            or existing.request_digest != digest
                            or existing.idempotency_key_digest != idem_digest
                        ):
                            raise JobIdentityConflict()
                        return DiscoveryRegistrationResult(existing, replayed=True)

                    pipe.multi()
                    pipe.set(idem_key, canonical_json_bytes(identity), ex=self.state_ttl_seconds)
                    pipe.set(request_key, canonical_json_bytes(request_payload), ex=self.state_ttl_seconds)
                    pipe.set(state_key, initial.model_dump_json(), ex=self.state_ttl_seconds)
                    pipe.zadd(self.keys.active, {str(discovery_job_id): now.timestamp()})
                    await pipe.execute()
                    break
                except WatchError:
                    continue

        await self.redis.publish(self.keys.events(discovery_job_id), str(initial.revision))
        return DiscoveryRegistrationResult(initial, replayed=False)

    async def get_state(
        self, discovery_job_id: UUID, *, client: Any | None = None
    ) -> CompetitorDiscoveryJobState | None:
        source = client or self.redis
        raw = await source.get(self.keys.state(discovery_job_id))
        return None if raw is None else CompetitorDiscoveryJobState.model_validate_json(raw)

    async def require_state(self, discovery_job_id: UUID) -> CompetitorDiscoveryJobState:
        state = await self.get_state(discovery_job_id)
        if state is None:
            raise JobNotFound()
        return state

    async def get_request(self, discovery_job_id: UUID) -> dict[str, Any] | None:
        raw = await self.redis.get(self.keys.request(discovery_job_id))
        if raw is None:
            return None
        value = _decode_json(raw)
        return value if isinstance(value, dict) else None

    async def transition(
        self,
        discovery_job_id: UUID,
        *,
        status: CompetitorDiscoveryStatus,
        stage: CompetitorDiscoveryStage,
        progress: int,
        message: str,
        now: datetime,
        attempt_count: int | None = None,
        heartbeat_at: datetime | None = None,
        result: CompetitorDiscoveryResult | None = None,
        error: CompetitorDiscoveryError | None = None,
    ) -> DiscoveryTransitionResult:
        state_key = self.keys.state(discovery_job_id)
        next_state: CompetitorDiscoveryJobState | None = None
        async with self.redis.pipeline(transaction=True) as pipe:
            while True:
                try:
                    await pipe.watch(state_key)
                    current = await self.get_state(discovery_job_id, client=pipe)
                    if current is None:
                        raise JobNotFound()
                    if current.terminal:
                        return DiscoveryTransitionResult(current, applied=False)
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
                            "attempt_count": current.attempt_count if attempt_count is None else attempt_count,
                            "revision": current.revision + 1,
                            "heartbeat_at": heartbeat_at or (now if status == "running" else current.heartbeat_at),
                            "updated_at": now,
                            "completed_at": completed_at,
                            "result": result,
                            "error": error,
                        }
                    )
                    next_state = CompetitorDiscoveryJobState.model_validate(next_state.model_dump())
                    pipe.multi()
                    pipe.set(state_key, next_state.model_dump_json(), ex=self.state_ttl_seconds)
                    pipe.expire(self.keys.request(discovery_job_id), self.state_ttl_seconds)
                    if next_state.terminal:
                        pipe.zrem(self.keys.active, str(discovery_job_id))
                    else:
                        pipe.zadd(self.keys.active, {str(discovery_job_id): now.timestamp()})
                    await pipe.execute()
                    break
                except WatchError:
                    continue
        assert next_state is not None
        await self.redis.publish(self.keys.events(discovery_job_id), str(next_state.revision))
        return DiscoveryTransitionResult(next_state, applied=True)

    async def retry_failed(self, discovery_job_id: UUID, *, now: datetime) -> CompetitorDiscoveryJobState:
        state_key = self.keys.state(discovery_job_id)
        checkpoint_keys = [
            key
            async for key in self.redis.scan_iter(
                match=self.keys.checkpoint_pattern(discovery_job_id)
            )
        ]
        next_state: CompetitorDiscoveryJobState | None = None
        async with self.redis.pipeline(transaction=True) as pipe:
            while True:
                try:
                    await pipe.watch(state_key)
                    current = await self.get_state(discovery_job_id, client=pipe)
                    if current is None:
                        raise JobNotFound()
                    if current.status != "failed" or current.error is None or not current.error.retryable:
                        raise JobNotRetryable()
                    next_state = current.model_copy(
                        update={
                            "status": "queued",
                            "stage": "queued",
                            "progress": 0,
                            "message": "Queued for a manual retry.",
                            "run_generation": current.run_generation + 1,
                            "revision": current.revision + 1,
                            "heartbeat_at": None,
                            "updated_at": now,
                            "completed_at": None,
                            "result": None,
                            "error": None,
                        }
                    )
                    next_state = CompetitorDiscoveryJobState.model_validate(next_state.model_dump())
                    pipe.multi()
                    pipe.set(state_key, next_state.model_dump_json(), ex=self.state_ttl_seconds)
                    pipe.expire(self.keys.request(discovery_job_id), self.state_ttl_seconds)
                    if checkpoint_keys:
                        # A manual retry is a fresh provider-attempt budget.
                        # Successful automatic retries still retain their
                        # checkpoints; only an explicitly reopened terminal
                        # task clears the exhausted ledger.
                        pipe.delete(*checkpoint_keys)
                    pipe.zadd(self.keys.active, {str(discovery_job_id): now.timestamp()})
                    await pipe.execute()
                    break
                except WatchError:
                    continue
        assert next_state is not None
        await self.redis.publish(self.keys.events(discovery_job_id), str(next_state.revision))
        return next_state

    async def active_count(self) -> int:
        return int(await self.redis.zcard(self.keys.active))

    async def list_stale_jobs(self, cutoff: datetime, *, limit: int = 100) -> list[UUID]:
        values = await self.redis.zrangebyscore(
            self.keys.active,
            min="-inf",
            max=cutoff.timestamp(),
            start=0,
            num=limit,
        )
        return [UUID(value.decode() if isinstance(value, bytes) else value) for value in values]
