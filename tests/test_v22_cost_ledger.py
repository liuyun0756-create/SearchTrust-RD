import asyncio
from datetime import datetime, timedelta, timezone
from uuid import UUID

import fakeredis.aioredis
import pytest

from app.jobs_v22.cost_ledger import (
    CostClaimConflict,
    CostLimitExceeded,
    JobCostLedger,
)
from app.jobs_v22.cost_models import PricingCatalog, empty_request_prices


JOB_ID = UUID("55555555-5555-4555-8555-555555555555")
NOW = datetime(2026, 9, 10, 8, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def redis() -> fakeredis.aioredis.FakeRedis:
    return fakeredis.aioredis.FakeRedis(decode_responses=False)


@pytest.fixture
def ledger(redis, clock) -> JobCostLedger:
    prices = empty_request_prices()
    prices["serpapi"] = 2_500
    return JobCostLedger(
        redis,
        prefix="test:v22",
        job_id=JOB_ID,
        ttl_seconds=3600,
        pricing=PricingCatalog(revision=3, request_usd_micros=prices),
        job_created_at=NOW,
        clock=clock,
    )


@pytest.mark.anyio
async def test_concurrent_claims_never_exceed_operation_limit(ledger) -> None:
    results = await asyncio.gather(
        *(ledger.claim("serpapi_market_search") for _ in range(40)),
        return_exceptions=True,
    )

    assert sum(not isinstance(result, Exception) for result in results) == 30
    assert sum(isinstance(result, CostLimitExceeded) for result in results) == 10
    state = await ledger.state()
    assert len(state.claims) == 30
    assert state.local_denials["serpapi_market_search"] == 10


@pytest.mark.anyio
async def test_disabled_operation_fails_closed_without_provider_attempt(ledger) -> None:
    with pytest.raises(CostLimitExceeded):
        await ledger.claim("jina_fetch")

    counters = (await ledger.snapshot()).root
    assert counters["jina_attempts"] == 0
    assert counters["jina_local_denials"] == 1


@pytest.mark.anyio
async def test_completion_is_idempotent_and_conflicting_replay_is_rejected(
    ledger, clock
) -> None:
    claim = await ledger.claim("serpapi_market_search")
    clock.now += timedelta(milliseconds=25)
    first = await ledger.complete(claim.claim_id, outcome="success", duration_ms=25)
    replay = await ledger.complete(claim.claim_id, outcome="success", duration_ms=25)

    assert replay == first
    counters = (await ledger.snapshot()).root
    assert counters["serpapi_attempts"] == 1
    assert counters["serpapi_successes"] == 1
    assert counters["serpapi_duration_ms"] == 25
    assert counters["estimated_cost_usd_micros"] == 2_500

    with pytest.raises(CostClaimConflict):
        await ledger.complete(claim.claim_id, outcome="failure", duration_ms=25)


@pytest.mark.anyio
async def test_pending_claim_is_conservatively_unknown(ledger) -> None:
    await ledger.claim("serpapi_market_search")

    counters = (await ledger.snapshot()).root

    assert counters["serpapi_outcome_unknown"] == 1
    assert counters["provider_outcome_unknown_total"] == 1
    assert counters["estimated_cost_usd_micros"] == 2_500


@pytest.mark.anyio
async def test_dify_usage_and_job_attempts_are_recorded(redis, clock) -> None:
    prices = empty_request_prices()
    prices["dify"] = 100
    value = JobCostLedger(
        redis,
        prefix="test:v22",
        job_id=JOB_ID,
        ttl_seconds=3600,
        pricing=PricingCatalog(
            revision=1,
            request_usd_micros=prices,
            dify_input_mtok_usd_micros=2_000_000,
            dify_output_mtok_usd_micros=6_000_000,
        ),
        job_created_at=NOW,
        clock=clock,
    )
    claim = await value.claim("dify_workflow")
    clock.now += timedelta(seconds=1)
    await value.complete(
        claim.claim_id,
        outcome="success",
        duration_ms=1000,
        usage_known=True,
        input_tokens=500_000,
        output_tokens=250_000,
        total_tokens=750_000,
    )
    await value.record_job_attempt(active_elapsed_ms=1000)
    await value.record_job_attempt(active_elapsed_ms=250)
    await value.record_checkpoint_hit(count=2)

    counters = (await value.snapshot()).root

    assert counters["dify_total_tokens"] == 750_000
    assert counters["job_attempts"] == 2
    assert counters["job_retry_count"] == 1
    assert counters["job_active_elapsed_ms"] == 1250
    assert counters["checkpoint_hits_total"] == 2
    assert counters["estimated_cost_usd_micros"] == 2_500_100


@pytest.mark.anyio
async def test_existing_ledger_keeps_original_pricing_across_worker_generation(
    redis, clock
) -> None:
    old_prices = empty_request_prices()
    old_prices["serpapi"] = 100
    first = JobCostLedger(
        redis,
        prefix="test:v22",
        job_id=JOB_ID,
        ttl_seconds=3600,
        pricing=PricingCatalog(revision=1, request_usd_micros=old_prices),
        job_created_at=NOW,
        clock=clock,
    )
    await first.claim("serpapi_market_search")

    new_prices = empty_request_prices()
    new_prices["serpapi"] = 999
    recovered = JobCostLedger(
        redis,
        prefix="test:v22",
        job_id=JOB_ID,
        ttl_seconds=3600,
        pricing=PricingCatalog(revision=2, request_usd_micros=new_prices),
        job_created_at=NOW,
        clock=clock,
    )

    counters = (await recovered.snapshot()).root
    assert counters["pricing_revision"] == 1
    assert counters["estimated_cost_usd_micros"] == 100


@pytest.mark.anyio
async def test_ledger_receives_configured_ttl(ledger, redis) -> None:
    await ledger.ensure()

    ttl = await redis.ttl(ledger.key)

    assert 3590 <= ttl <= 3600
