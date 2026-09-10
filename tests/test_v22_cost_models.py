from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.jobs_v22.cost_models import (
    ALLOWED_COUNTER_KEYS,
    CostClaim,
    CostCountersV1,
    CostLedgerState,
    OPERATION_LIMITS,
    PricingCatalog,
    cost_counters_from_state,
    empty_request_prices,
    microdollars_for_tokens,
    pricing_catalog_from_settings,
)


JOB_ID = UUID("55555555-5555-4555-8555-555555555555")
CLAIM_ID = UUID("66666666-6666-4666-8666-666666666666")
NOW = datetime(2026, 9, 10, 8, 0, tzinfo=timezone.utc)


def pricing(**overrides: object) -> PricingCatalog:
    values: dict[str, object] = {
        "revision": 1,
        "request_usd_micros": empty_request_prices(),
    }
    values.update(overrides)
    return PricingCatalog(**values)


def test_approved_operation_limits_are_fixed() -> None:
    assert OPERATION_LIMITS == {
        "serpapi_location": 1,
        "serpapi_market_search": 30,
        "serpapi_public_profile": 15,
        "firecrawl_map": 4,
        "jina_fetch": 0,
        "pagespeed_analysis": 5,
        "gsc_search_analytics": 12,
        "ga4_key_events": 20,
        "ga4_run_report": 8,
        "gbp_location": 1,
        "gbp_performance": 1,
        "gbp_keywords": 10,
        "dify_workflow": 3,
    }


def test_pricing_catalog_requires_every_provider() -> None:
    values = empty_request_prices()
    values.pop("jina")

    with pytest.raises(ValidationError, match="every provider"):
        PricingCatalog(request_usd_micros=values)


def test_claim_rejects_wrong_provider_and_invalid_token_usage() -> None:
    with pytest.raises(ValidationError, match="does not belong"):
        CostClaim(
            claim_id=CLAIM_ID,
            operation="firecrawl_map",
            provider="serpapi",
            claimed_at=NOW,
        )

    with pytest.raises(ValidationError, match="total tokens"):
        CostClaim(
            claim_id=CLAIM_ID,
            operation="dify_workflow",
            provider="dify",
            claimed_at=NOW,
            completed_at=NOW,
            outcome="success",
            duration_ms=1,
            usage_known=True,
            input_tokens=10,
            output_tokens=5,
            total_tokens=14,
        )


def test_token_cost_uses_conservative_integer_rounding() -> None:
    assert microdollars_for_tokens(1, 1) == 1
    assert microdollars_for_tokens(500_000, 2_000_000) == 1_000_000
    assert microdollars_for_tokens(0, 2_000_000) == 0


def test_snapshot_counts_failures_unknowns_tokens_and_prices() -> None:
    request_prices = empty_request_prices()
    request_prices.update({"serpapi": None, "dify": 1_000})
    state = CostLedgerState(
        job_id=JOB_ID,
        revision=5,
        pricing=pricing(
            revision=7,
            request_usd_micros=request_prices,
            dify_input_mtok_usd_micros=2_000_000,
            dify_output_mtok_usd_micros=6_000_000,
        ),
        created_at=NOW,
        updated_at=NOW + timedelta(seconds=2),
        checkpoint_hits=3,
        job_attempts=2,
        job_active_elapsed_ms=1_500,
        claims=[
            CostClaim(
                claim_id=CLAIM_ID,
                operation="serpapi_market_search",
                provider="serpapi",
                claimed_at=NOW,
                completed_at=NOW + timedelta(milliseconds=100),
                outcome="failure",
                duration_ms=100,
            ),
            CostClaim(
                claim_id=UUID("77777777-7777-4777-8777-777777777777"),
                operation="dify_workflow",
                provider="dify",
                claimed_at=NOW,
                completed_at=NOW + timedelta(seconds=1),
                outcome="success",
                duration_ms=1_000,
                usage_known=True,
                input_tokens=500_000,
                output_tokens=250_000,
                total_tokens=750_000,
            ),
            CostClaim(
                claim_id=UUID("88888888-8888-4888-8888-888888888888"),
                operation="dify_workflow",
                provider="dify",
                claimed_at=NOW + timedelta(seconds=2),
            ),
        ],
        local_denials={"firecrawl_map": 2},
    )

    counters = cost_counters_from_state(state, now=NOW + timedelta(seconds=3)).root

    assert set(counters) == ALLOWED_COUNTER_KEYS
    assert counters["provider_attempts_total"] == 3
    assert counters["provider_successes_total"] == 1
    assert counters["provider_failures_total"] == 1
    assert counters["provider_outcome_unknown_total"] == 1
    assert counters["provider_local_denials_total"] == 2
    assert counters["dify_total_tokens"] == 750_000
    assert counters["dify_usage_unknown_calls"] == 1
    assert counters["estimated_cost_usd_micros"] == 2_502_000
    assert counters["pricing_unknown_units_total"] == 1
    assert counters["job_retry_count"] == 1
    assert counters["checkpoint_hits_total"] == 3


def test_counter_contract_rejects_missing_unknown_and_non_integer_values() -> None:
    valid = {key: 0 for key in ALLOWED_COUNTER_KEYS}
    valid["cost_schema_version"] = 1
    valid["cost_ledger_revision"] = 1
    valid["pricing_revision"] = 1
    assert CostCountersV1(valid).root == valid

    with pytest.raises(ValidationError, match="complete V1"):
        CostCountersV1({"cost_schema_version": 1})
    with pytest.raises(ValidationError, match="unknown key"):
        CostCountersV1({**valid, "customer_url": 1})
    with pytest.raises(ValidationError, match="valid integer"):
        CostCountersV1({**valid, "job_attempts": 1.5})


def test_settings_freeze_unknown_and_known_prices() -> None:
    catalog = pricing_catalog_from_settings(
        Settings(
            V22_COST_PRICING_REVISION=9,
            V22_COST_SERPAPI_REQUEST_USD_MICROS=2500,
            V22_COST_GSC_REQUEST_USD_MICROS=0,
            V22_COST_DIFY_INPUT_MTOK_USD_MICROS=2_000_000,
        )
    )

    assert catalog.revision == 9
    assert catalog.request_usd_micros["serpapi"] == 2500
    assert catalog.request_usd_micros["gsc"] == 0
    assert catalog.request_usd_micros["firecrawl"] is None
    assert catalog.dify_input_mtok_usd_micros == 2_000_000
