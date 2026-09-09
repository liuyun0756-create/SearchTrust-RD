"""Redis-shared circuit breakers for bounded provider calls."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from redis.asyncio import Redis

from app.jobs_v22.errors import ProviderCircuitOpen
from app.jobs_v22.keys import JobRedisKeys


@dataclass(frozen=True)
class CircuitPermit:
    provider: str
    operation: str
    probe: bool


class RedisCircuitBreaker:
    """A small shared closed/open/half-open state machine.

    Callers may use a SerpAPI key fingerprint as part of ``provider`` to isolate
    an individual key without exposing the secret.
    """

    def __init__(
        self,
        redis: Redis,
        *,
        prefix: str,
        failure_threshold: int = 5,
        window_seconds: int = 60,
        cooldown_seconds: tuple[int, ...] = (60, 120, 300),
    ) -> None:
        self.redis = redis
        self.keys = JobRedisKeys(prefix)
        self.failure_threshold = failure_threshold
        self.window_seconds = window_seconds
        self.cooldown_seconds = cooldown_seconds

    async def before_call(
        self,
        provider: str,
        operation: str,
        *,
        now: datetime,
    ) -> CircuitPermit:
        key = self.keys.circuit(provider, operation)
        raw = await self.redis.get(key)
        state = json.loads(raw) if raw else {"status": "closed", "open_count": 0}
        if state["status"] == "closed":
            return CircuitPermit(provider, operation, False)

        open_until = float(state.get("open_until", 0))
        if now.timestamp() < open_until:
            raise ProviderCircuitOpen()

        probe_key = f"{key}:probe"
        claimed = await self.redis.set(
            probe_key,
            str(uuid4()),
            nx=True,
            ex=max(self.cooldown_seconds[0], 30),
        )
        if not claimed:
            raise ProviderCircuitOpen()
        return CircuitPermit(provider, operation, True)

    async def record_success(self, permit: CircuitPermit) -> None:
        key = self.keys.circuit(permit.provider, permit.operation)
        await self.redis.delete(
            key,
            f"{key}:probe",
            self.keys.circuit_failures(permit.provider, permit.operation),
        )

    async def record_failure(
        self,
        permit: CircuitPermit,
        *,
        now: datetime,
        eligible: bool = True,
        immediate: bool = False,
        failure_threshold: int | None = None,
    ) -> bool:
        """Return whether this failure leaves the circuit open."""

        if not eligible:
            if permit.probe:
                await self.redis.delete(
                    f"{self.keys.circuit(permit.provider, permit.operation)}:probe"
                )
            return False

        key = self.keys.circuit(permit.provider, permit.operation)
        failures = self.keys.circuit_failures(permit.provider, permit.operation)
        cutoff = now.timestamp() - self.window_seconds
        member = f"{now.timestamp()}:{uuid4()}"
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.zremrangebyscore(failures, "-inf", cutoff)
            pipe.zadd(failures, {member: now.timestamp()})
            pipe.expire(failures, self.window_seconds * 2)
            await pipe.execute()
        count = int(await self.redis.zcard(failures))
        current_raw = await self.redis.get(key)
        current = json.loads(current_raw) if current_raw else {"open_count": 0}
        should_open = immediate or permit.probe or count >= (failure_threshold or self.failure_threshold)
        if not should_open:
            return False

        open_count = int(current.get("open_count", 0)) + 1
        cooldown = self.cooldown_seconds[min(open_count - 1, len(self.cooldown_seconds) - 1)]
        state = {
            "status": "open",
            "open_count": open_count,
            "open_until": now.timestamp() + cooldown,
        }
        await self.redis.set(key, json.dumps(state, separators=(",", ":")), ex=cooldown + self.window_seconds)
        await self.redis.delete(f"{key}:probe")
        return True
