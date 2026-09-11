from __future__ import annotations

import asyncio
from datetime import timedelta
from uuid import UUID

import pytest

from app.jobs_v22.cost_ledger import JobCostLedger
from app.jobs_v22.cost_models import (
    CostSummaryRecord,
    PricingCatalog,
    empty_request_prices,
)
from app.jobs_v22.cost_persistence import CostSummaryOutbox
from app.jobs_v22.models import utc_now


pytestmark = [pytest.mark.redis_integration, pytest.mark.anyio]

JOB_ID = UUID("55555555-5555-4555-8555-555555555556")
CASE_ID = UUID("11111111-1111-4111-8111-111111111111")
CLAIM_ID = UUID("77777777-7777-4777-8777-777777777777")


class IdempotentPersister:
    def __init__(self) -> None:
        self.attempts = 0
        self.settlements: set[tuple[UUID, int]] = set()

    async def persist(self, summary: CostSummaryRecord) -> bool:
        self.attempts += 1
        self.settlements.add((summary.job_id, summary.ledger_revision))
        return True


async def test_cost_claim_and_summary_settle_once_across_restart_and_duplicate_sync(
    redis_client,
    redis_prefix: str,
) -> None:
    started = utc_now()
    prices = empty_request_prices()
    prices["serpapi"] = 2_500
    pricing = PricingCatalog(revision=1, request_usd_micros=prices)
    first = JobCostLedger(
        redis_client,
        prefix=redis_prefix,
        job_id=JOB_ID,
        ttl_seconds=3600,
        pricing=pricing,
        job_created_at=started,
        clock=lambda: started,
    )
    claim = await first.claim("serpapi_market_search", claim_id=CLAIM_ID)

    recovered = JobCostLedger(
        redis_client,
        prefix=redis_prefix,
        job_id=JOB_ID,
        ttl_seconds=3600,
        pricing=pricing,
        job_created_at=started,
        clock=lambda: started + timedelta(seconds=1),
    )
    completed = await recovered.complete(
        claim.claim_id,
        outcome="success",
        duration_ms=1000,
    )
    replay = await recovered.complete(
        claim.claim_id,
        outcome="success",
        duration_ms=1000,
    )
    counters = await recovered.snapshot()
    assert replay == completed
    assert counters.root["serpapi_attempts"] == 1
    assert counters.root["serpapi_successes"] == 1
    assert counters.root["estimated_cost_usd_micros"] == 2_500

    summary = CostSummaryRecord(
        job_id=JOB_ID,
        case_id=CASE_ID,
        job_kind="prospect_report",
        status="succeeded",
        attempt_count=1,
        ledger_revision=counters.root["cost_ledger_revision"],
        cost_counters=counters,
        started_at=started,
        completed_at=started + timedelta(seconds=1),
    )
    persister = IdempotentPersister()
    outbox = CostSummaryOutbox(
        redis_client,
        prefix=redis_prefix,
        ttl_seconds=3600,
        persister=persister,  # type: ignore[arg-type]
    )
    await outbox.enqueue(summary)
    await asyncio.gather(outbox.sync(JOB_ID), outbox.sync(JOB_ID))
    await outbox.sync(JOB_ID)

    assert persister.settlements == {(JOB_ID, summary.ledger_revision)}
    assert await redis_client.zcard(outbox.keys.cost_sync_pending) == 0
    assert await redis_client.get(outbox.keys.cost_summary(JOB_ID)) is None
