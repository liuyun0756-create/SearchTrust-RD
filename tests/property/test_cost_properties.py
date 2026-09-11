from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

import fakeredis.aioredis
import pytest
from hypothesis import given, settings, strategies as st

from app.jobs_v22.cost_ledger import CostClaimConflict, JobCostLedger
from app.jobs_v22.cost_models import (
    CostClaim,
    CostLedgerState,
    PricingCatalog,
    cost_counters_from_state,
    empty_request_prices,
    microdollars_for_tokens,
)
from property.strategies import MONEY_MICROS, PROVIDER_OUTCOMES


JOB_ID = UUID("55555555-5555-4555-8555-555555555555")
NOW = datetime(2026, 9, 11, 8, 0, tzinfo=timezone.utc)


@given(
    st.integers(min_value=0, max_value=100_000_000),
    MONEY_MICROS,
)
def test_token_cost_is_nonnegative_and_uses_conservative_ceiling(tokens, rate) -> None:
    result = microdollars_for_tokens(tokens, rate)
    assert result >= 0
    assert result * 1_000_000 >= tokens * rate
    if result:
        assert (result - 1) * 1_000_000 < tokens * rate


@given(
    PROVIDER_OUTCOMES,
    st.lists(st.integers(min_value=1, max_value=100), min_size=1, max_size=12),
    MONEY_MICROS,
)
def test_provider_outcome_sequences_partition_once_without_cost_underflow(
    outcomes, units, rate
) -> None:
    units = (units * len(outcomes))[: len(outcomes)]
    prices = empty_request_prices()
    prices["serpapi"] = rate
    claims = []
    for index, (outcome, billable_units) in enumerate(zip(outcomes, units, strict=True)):
        completed = outcome != "unknown"
        claims.append(CostClaim(
            claim_id=UUID(int=index + 1),
            operation="serpapi_market_search",
            provider="serpapi",
            claimed_at=NOW,
            completed_at=NOW + timedelta(milliseconds=index + 1) if completed else None,
            outcome=outcome if completed else None,
            duration_ms=index + 1 if completed else None,
            billable_units=billable_units,
        ))
    state = CostLedgerState(
        job_id=JOB_ID,
        pricing=PricingCatalog(request_usd_micros=prices),
        created_at=NOW,
        updated_at=NOW + timedelta(seconds=1),
        claims=claims,
    )

    counters = cost_counters_from_state(state, now=NOW + timedelta(seconds=2)).root
    assert counters["provider_attempts_total"] == len(outcomes)
    assert (
        counters["provider_successes_total"]
        + counters["provider_failures_total"]
        + counters["provider_outcome_unknown_total"]
    ) == len(outcomes)
    assert counters["estimated_cost_usd_micros"] == rate * sum(units)
    assert all(value >= 0 for value in counters.values())


@pytest.mark.anyio
@given(st.sampled_from(("success", "failure")), st.integers(min_value=1, max_value=8))
@settings(max_examples=20)
async def test_claim_completion_replay_is_charged_once_and_conflicts_fail_closed(
    outcome, replay_count
) -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    prices = empty_request_prices()
    prices["serpapi"] = 2_500
    ledger = JobCostLedger(
        redis,
        prefix="property:v22",
        job_id=JOB_ID,
        ttl_seconds=3_600,
        pricing=PricingCatalog(request_usd_micros=prices),
        job_created_at=NOW,
        clock=lambda: NOW + timedelta(milliseconds=25),
    )
    claim_id = UUID("66666666-6666-4666-8666-666666666666")
    await ledger.claim("serpapi_market_search", claim_id=claim_id)
    for _ in range(replay_count):
        await ledger.complete(claim_id, outcome=outcome, duration_ms=25)

    counters = (await ledger.snapshot()).root
    assert counters["serpapi_attempts"] == 1
    assert counters["serpapi_successes"] + counters["serpapi_failures"] == 1
    assert counters["estimated_cost_usd_micros"] == 2_500
    opposite = "failure" if outcome == "success" else "success"
    with pytest.raises(CostClaimConflict):
        await ledger.complete(claim_id, outcome=opposite, duration_ms=25)
