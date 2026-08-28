from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from pydantic import ValidationError
import pytest

from app.collectors.serp_market_models import (
    SerpCallRecord,
    SerpMarketBudget,
    SerpMarketResultRecord,
    SerpMarketSnapshot,
    SerpQueryRun,
    SerpResultCount,
    SerpTargetPoint,
)


NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64


def target_point() -> SerpTargetPoint:
    return SerpTargetPoint(
        requested_label="Austin, TX",
        canonical_name="Austin,Texas,United States",
        country_code="US",
        latitude=30.2672,
        longitude=-97.7431,
        source="explicit_coordinates",
        resolved_at=NOW,
    )


def call(query: str, engine: str, *, digest: str, status: str = "succeeded") -> SerpCallRecord:
    succeeded = status == "succeeded"
    return SerpCallRecord(
        call_id=digest,
        request_digest=digest,
        query=query,
        engine=engine,
        result_types=["maps"] if engine == "google_maps" else ["local_pack", "organic"],
        latitude=30.2672,
        longitude=-97.7431,
        country_code="US",
        language="en",
        device="mobile",
        maps_zoom=14 if engine == "google_maps" else None,
        status=status,
        started_at=NOW,
        completed_at=NOW,
        provider_search_id="search-1" if succeeded else None,
        response_checksum=DIGEST_A if succeeded else None,
        logical_call_used=True,
        provider_attempts=1,
        error_code=None if succeeded else "SERP_PROVIDER_TEMPORARY",
    )


def result(query: str, *, call_id: str = DIGEST_A) -> SerpMarketResultRecord:
    record_id = "sha256:" + call_id[-1] * 63 + str((len(query) % 10))
    return SerpMarketResultRecord(
        record_id=record_id,
        call_id=call_id,
        query=query,
        result_type="maps",
        position=1,
        rank_source="provider_position",
        display_name="Acme Plumbing",
        url="https://www.example.com/service",
        normalized_domain="example.com",
        categories=["Plumber"],
        rating=4.8,
        review_count=120,
        response_checksum=DIGEST_A,
        observed_at=NOW,
        latitude=30.2672,
        longitude=-97.7431,
        country_code="US",
        language="en",
        device="mobile",
    )


def query_run(index: int, query: str) -> SerpQueryRun:
    maps_digest = "sha256:" + f"{index:x}" * 64
    google_digest = "sha256:" + f"{index + 5:x}" * 64
    maps_call = call(query, "google_maps", digest=maps_digest)
    google_call = call(query, "google", digest=google_digest)
    return SerpQueryRun(
        query_index=index,
        query=query,
        maps_call=maps_call,
        google_call=google_call,
        results=[result(query, call_id=maps_digest)],
        complete=True,
    )


def snapshot() -> SerpMarketSnapshot:
    queries = ["plumber", "emergency plumber", "water heater repair"]
    runs = [query_run(index, query) for index, query in enumerate(queries, start=1)]
    return SerpMarketSnapshot(
        schema_version="serp_market_snapshot_v1",
        job_id=UUID("00000000-0000-0000-0000-000000000022"),
        started_at=NOW,
        completed_at=NOW,
        target_point=target_point(),
        device="mobile",
        language="en",
        country_code="US",
        maps_zoom=14,
        queries=queries,
        query_runs=runs,
        result_counts=[
            SerpResultCount(result_type="maps", count=3),
            SerpResultCount(result_type="local_pack", count=0),
            SerpResultCount(result_type="organic", count=0),
        ],
        complete_query_count=3,
        budget=SerpMarketBudget(
            logical_calls_planned=6,
            logical_calls_used=6,
            provider_attempt_limit=18,
            provider_attempts=6,
            checkpoint_hits=0,
            location_resolution_calls=0,
        ),
    )


def test_serp_market_snapshot_accepts_strict_complete_contract() -> None:
    value = snapshot()

    assert value.complete_query_count == 3
    assert value.budget.logical_call_limit == 10
    assert value.query_runs[0].results[0].normalized_domain == "example.com"


def test_provider_location_requires_auditable_resolution_fields() -> None:
    with pytest.raises(ValidationError, match="provider locations require"):
        SerpTargetPoint(
            requested_label="Austin",
            canonical_name="Austin,Texas,United States",
            country_code="US",
            latitude=30.2672,
            longitude=-97.7431,
            source="serpapi_location",
            resolved_at=NOW,
        )


def test_query_run_rejects_result_from_wrong_call() -> None:
    with pytest.raises(ValidationError, match="compatible query call"):
        SerpQueryRun(
            query_index=1,
            query="plumber",
            maps_call=call("plumber", "google_maps", digest=DIGEST_A),
            google_call=call("plumber", "google", digest=DIGEST_B),
            results=[result("plumber", call_id="sha256:" + "c" * 64)],
            complete=True,
        )


def test_budget_rejects_attempt_limit_that_does_not_match_plan() -> None:
    with pytest.raises(ValidationError, match="attempt limit"):
        SerpMarketBudget(
            logical_calls_planned=6,
            logical_calls_used=0,
            provider_attempt_limit=30,
            provider_attempts=0,
            checkpoint_hits=0,
            location_resolution_calls=0,
        )


def test_snapshot_rejects_fewer_than_three_complete_queries() -> None:
    raw = snapshot().model_dump(mode="python")
    raw["query_runs"][0]["maps_call"]["status"] = "failed_transient"
    raw["query_runs"][0]["maps_call"]["response_checksum"] = None
    raw["query_runs"][0]["maps_call"]["error_code"] = "SERP_PROVIDER_TEMPORARY"
    raw["query_runs"][0]["complete"] = False
    raw["complete_query_count"] = 2

    with pytest.raises(ValidationError):
        SerpMarketSnapshot.model_validate(raw)


def test_models_reject_unknown_fields_and_mismatched_domains() -> None:
    raw = result("plumber").model_dump(mode="python")
    raw["secret"] = "should-not-be-accepted"
    with pytest.raises(ValidationError):
        SerpMarketResultRecord.model_validate(raw)

    raw.pop("secret")
    raw["normalized_domain"] = "attacker.example"
    with pytest.raises(ValidationError, match="normalized domain"):
        SerpMarketResultRecord.model_validate(raw)


def test_snapshot_rejects_duplicate_stable_record_ids() -> None:
    raw = snapshot().model_dump(mode="python")
    raw["query_runs"][1]["results"][0]["record_id"] = raw["query_runs"][0]["results"][0][
        "record_id"
    ]

    with pytest.raises(ValidationError, match="record IDs must be unique"):
        SerpMarketSnapshot.model_validate(raw)
