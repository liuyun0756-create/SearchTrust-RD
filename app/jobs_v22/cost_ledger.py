"""Atomic Redis ledger for one logical V2.2 job across worker generations."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

from pydantic import ValidationError
from redis.exceptions import WatchError

from app.jobs_v22.cost_models import (
    CostClaim,
    CostCountersV1,
    CostLedgerState,
    CostOperation,
    CostOutcome,
    OPERATION_LIMITS,
    OPERATION_PROVIDER,
    PricingCatalog,
    cost_counters_from_state,
)
from app.jobs_v22.keys import JobRedisKeys
from app.jobs_v22.errors import DeterministicJobError, TransientJobError


class CostLedgerError(TransientJobError):
    error_code = "V22_COST_LEDGER_UNAVAILABLE"

    def __init__(self, error_code: str | None = None) -> None:
        super().__init__(
            error_code or self.error_code,
            "Cost controls are temporarily unavailable. The task will be retried.",
        )


class CostLimitExceeded(DeterministicJobError):
    error_code = "V22_PROVIDER_ATTEMPT_LIMIT"

    def __init__(self, operation: CostOperation) -> None:
        super().__init__(
            self.error_code,
            "The task reached its safe provider request limit and could not be completed.",
        )
        self.operation = operation


class CostClaimConflict(CostLedgerError):
    error_code = "V22_COST_CLAIM_CONFLICT"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class JobCostLedger:
    def __init__(
        self,
        redis,
        *,
        prefix: str,
        job_id: UUID,
        ttl_seconds: int,
        pricing: PricingCatalog,
        job_created_at: datetime,
        clock=None,
    ) -> None:
        if ttl_seconds < 60:
            raise ValueError("cost ledger TTL is too short")
        if job_created_at.tzinfo is None:
            raise ValueError("job creation time must be timezone-aware")
        self.redis = redis
        self.keys = JobRedisKeys(prefix)
        self.job_id = job_id
        self.ttl_seconds = ttl_seconds
        self.pricing = pricing
        self.job_created_at = job_created_at
        self.clock = clock or _utc_now

    @property
    def key(self) -> str:
        return self.keys.cost_ledger(self.job_id)

    def _new_state(self, now: datetime) -> CostLedgerState:
        return CostLedgerState(
            job_id=self.job_id,
            pricing=self.pricing,
            created_at=self.job_created_at,
            updated_at=max(now, self.job_created_at),
        )

    def _decode(self, raw, now: datetime) -> CostLedgerState:
        if raw is None:
            return self._new_state(now)
        try:
            state = CostLedgerState.model_validate_json(raw)
        except (TypeError, ValueError, ValidationError) as exc:
            raise CostLedgerError(CostLedgerError.error_code) from exc
        if state.job_id != self.job_id:
            raise CostClaimConflict(CostClaimConflict.error_code)
        return state

    async def _mutate(self, operation):
        async with self.redis.pipeline(transaction=True) as pipe:
            while True:
                try:
                    await pipe.watch(self.key)
                    now = self.clock()
                    if now.tzinfo is None:
                        raise CostLedgerError(CostLedgerError.error_code)
                    current = self._decode(await pipe.get(self.key), now)
                    updated, result = operation(current, now)
                    updated = CostLedgerState.model_validate(updated.model_dump(mode="python"))
                    pipe.multi()
                    pipe.set(self.key, updated.model_dump_json(), ex=self.ttl_seconds)
                    await pipe.execute()
                    return result
                except WatchError:
                    continue

    async def ensure(self) -> CostLedgerState:
        return await self._mutate(lambda state, now: (state, state))

    async def claim(
        self,
        operation: CostOperation,
        *,
        claim_id: UUID | None = None,
    ) -> CostClaim:
        provider = OPERATION_PROVIDER[operation]
        limit = OPERATION_LIMITS[operation]
        requested_id = claim_id or uuid4()

        def mutate(state: CostLedgerState, now: datetime):
            existing = next((item for item in state.claims if item.claim_id == requested_id), None)
            if existing is not None:
                if existing.operation != operation:
                    raise CostClaimConflict(CostClaimConflict.error_code)
                return state, existing
            used = sum(item.operation == operation for item in state.claims)
            if used >= limit:
                denials = dict(state.local_denials)
                denials[operation] = denials.get(operation, 0) + 1
                updated = state.model_copy(
                    update={
                        "revision": state.revision + 1,
                        "updated_at": now,
                        "local_denials": denials,
                    }
                )
                return updated, None
            claim = CostClaim(
                claim_id=requested_id,
                operation=operation,
                provider=provider,
                claimed_at=now,
            )
            updated = state.model_copy(
                update={
                    "revision": state.revision + 1,
                    "updated_at": now,
                    "claims": [*state.claims, claim],
                }
            )
            return updated, claim

        result = await self._mutate(mutate)
        if result is None:
            raise CostLimitExceeded(operation)
        return result

    async def complete(
        self,
        claim_id: UUID,
        *,
        outcome: CostOutcome,
        duration_ms: int,
        billable_units: int = 1,
        usage_known: bool = False,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        total_tokens: int | None = None,
    ) -> CostClaim:
        def mutate(state: CostLedgerState, now: datetime):
            index = next(
                (position for position, item in enumerate(state.claims) if item.claim_id == claim_id),
                None,
            )
            if index is None:
                raise CostClaimConflict(CostClaimConflict.error_code)
            current = state.claims[index]
            completed = current.model_copy(
                update={
                    "completed_at": now,
                    "outcome": outcome,
                    "duration_ms": duration_ms,
                    "billable_units": billable_units,
                    "usage_known": usage_known,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "total_tokens": total_tokens,
                }
            )
            completed = CostClaim.model_validate(completed.model_dump(mode="python"))
            if current.completed:
                comparable_current = current.model_copy(update={"completed_at": completed.completed_at})
                if comparable_current != completed:
                    raise CostClaimConflict(CostClaimConflict.error_code)
                return state, current
            claims = list(state.claims)
            claims[index] = completed
            updated = state.model_copy(
                update={
                    "revision": state.revision + 1,
                    "updated_at": now,
                    "claims": claims,
                }
            )
            return updated, completed

        return await self._mutate(mutate)

    async def record_checkpoint_hit(self, *, count: int = 1) -> None:
        if isinstance(count, bool) or not 1 <= count <= 10_000:
            raise ValueError("checkpoint hit count is invalid")

        def mutate(state: CostLedgerState, now: datetime):
            updated = state.model_copy(
                update={
                    "revision": state.revision + 1,
                    "updated_at": now,
                    "checkpoint_hits": state.checkpoint_hits + count,
                }
            )
            return updated, None

        await self._mutate(mutate)

    async def record_job_attempt(self, *, active_elapsed_ms: int) -> None:
        if isinstance(active_elapsed_ms, bool) or not 0 <= active_elapsed_ms <= 86_400_000:
            raise ValueError("active attempt duration is invalid")

        def mutate(state: CostLedgerState, now: datetime):
            updated = state.model_copy(
                update={
                    "revision": state.revision + 1,
                    "updated_at": now,
                    "job_attempts": state.job_attempts + 1,
                    "job_active_elapsed_ms": state.job_active_elapsed_ms + active_elapsed_ms,
                }
            )
            return updated, None

        await self._mutate(mutate)

    async def state(self) -> CostLedgerState:
        raw = await self.redis.get(self.key)
        if raw is None:
            return await self.ensure()
        return self._decode(raw, self.clock())

    async def snapshot(self) -> CostCountersV1:
        return cost_counters_from_state(await self.state(), now=self.clock())
