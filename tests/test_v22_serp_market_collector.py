from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import pytest

from app.collectors.serp_market import (
    SerpMarketCollectionError,
    SerpMarketCollector,
    SerpMarketProviderError,
    SerpProviderResponse,
)
from app.collectors.serp_market_models import SerpTargetPoint
from app.collectors.serp_market_requests import SerpPlannedCall, build_serp_search_plan


FIXTURES = Path(__file__).parent / "fixtures" / "v22_serp_market"
NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
JOB_ID = UUID("00000000-0000-0000-0000-000000000022")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def plan(query_count: int = 3):
    queries = [
        "emergency plumber",
        "24 hour plumber",
        "water heater repair",
        "drain cleaning",
        "local plumber",
    ][:query_count]
    point = SerpTargetPoint(
        requested_label="Austin, TX",
        canonical_name="Austin, TX",
        country_code="US",
        latitude=30.2672,
        longitude=-97.7431,
        source="explicit_coordinates",
        resolved_at=NOW,
    )
    return build_serp_search_plan(queries=queries, target_point=point)


class FixtureProvider:
    def __init__(self, *, failures: set[tuple[str, str]] | None = None) -> None:
        self.failures = failures or set()
        self.calls: list[SerpPlannedCall] = []

    async def search(self, call: SerpPlannedCall) -> SerpProviderResponse:
        self.calls.append(call)
        if (call.query, call.engine) in self.failures:
            raise SerpMarketProviderError(
                "SERP_PROVIDER_TEMPORARY",
                retryable=True,
                provider_attempts=1,
            )
        fixture = "google-maps.json" if call.engine == "google_maps" else "google-search.json"
        return SerpProviderResponse(load_fixture(fixture))


@pytest.mark.anyio
async def test_collector_normalizes_three_result_types_without_cross_type_dedupe() -> None:
    provider = FixtureProvider()
    snapshot = await SerpMarketCollector(provider=provider, clock=lambda: NOW).collect(
        job_id=JOB_ID,
        plan=plan(),
        location_resolution_calls=0,
    )

    assert len(provider.calls) == 6
    assert {item.result_type: item.count for item in snapshot.result_counts} == {
        "maps": 6,
        "local_pack": 6,
        "organic": 6,
    }
    first = snapshot.query_runs[0]
    assert [result.result_type for result in first.results] == [
        "maps",
        "maps",
        "local_pack",
        "local_pack",
        "organic",
        "organic",
    ]
    assert sum("Acme" in result.display_name for result in first.results) == 3
    assert first.results[0].normalized_domain == "acme.example"
    assert first.results[0].provider_cid == "1110"
    assert snapshot.budget.logical_calls_used == 6
    assert snapshot.budget.provider_attempts == 6


@pytest.mark.anyio
async def test_successful_empty_provider_results_complete_query_groups() -> None:
    class EmptyProvider:
        async def search(self, call: SerpPlannedCall) -> SerpProviderResponse:
            return SerpProviderResponse(load_fixture("empty.json"))

    snapshot = await SerpMarketCollector(provider=EmptyProvider(), clock=lambda: NOW).collect(
        job_id=JOB_ID,
        plan=plan(),
        location_resolution_calls=0,
    )

    assert snapshot.complete_query_count == 3
    assert sum(item.count for item in snapshot.result_counts) == 0
    assert snapshot.limitations == []


@pytest.mark.anyio
async def test_collector_uses_response_order_and_bounds_each_result_type() -> None:
    class LargeProvider:
        async def search(self, call: SerpPlannedCall) -> SerpProviderResponse:
            payload = load_fixture("malformed.json") if call.engine == "google_maps" else {
                "search_metadata": {"id": "large", "status": "Success"},
                "local_results": [
                    {"title": f"Local {index}", "website": f"https://local{index}.example/"}
                    for index in range(25)
                ],
                "organic_results": [
                    {"title": f"Organic {index}", "link": f"https://organic{index}.example/"}
                    for index in range(25)
                ],
            }
            return SerpProviderResponse(payload)

    snapshot = await SerpMarketCollector(provider=LargeProvider(), clock=lambda: NOW).collect(
        job_id=JOB_ID,
        plan=plan(),
        location_resolution_calls=0,
    )

    first = snapshot.query_runs[0]
    assert sum(result.result_type == "maps" for result in first.results) == 2
    assert sum(result.result_type == "local_pack" for result in first.results) == 20
    assert sum(result.result_type == "organic" for result in first.results) == 20
    assert first.results[0].rank_source == "response_order"
    assert first.results[0].position == 2
    assert "SERP_MAPS_RESULT_SKIPPED" in first.limitations


@pytest.mark.anyio
async def test_five_query_collection_succeeds_with_three_complete_groups() -> None:
    search_plan = plan(5)
    provider = FixtureProvider(
        failures={
            (search_plan.queries[3], "google"),
            (search_plan.queries[4], "google_maps"),
        }
    )

    snapshot = await SerpMarketCollector(provider=provider, clock=lambda: NOW).collect(
        job_id=JOB_ID,
        plan=search_plan,
        location_resolution_calls=1,
    )

    assert snapshot.complete_query_count == 3
    assert len(provider.calls) == 10
    assert snapshot.budget.logical_calls_planned == 10
    assert snapshot.budget.location_resolution_calls == 1
    assert "SERP_PROVIDER_TEMPORARY" in snapshot.limitations


@pytest.mark.anyio
async def test_collection_rejects_fewer_than_three_complete_groups() -> None:
    search_plan = plan()
    provider = FixtureProvider(failures={(search_plan.queries[0], "google_maps")})

    with pytest.raises(SerpMarketCollectionError) as raised:
        await SerpMarketCollector(provider=provider, clock=lambda: NOW).collect(
            job_id=JOB_ID,
            plan=search_plan,
            location_resolution_calls=0,
        )

    assert raised.value.code == "SERP_MARKET_INSUFFICIENT_COMPLETE_QUERIES"
    assert raised.value.retryable is True
    assert len(raised.value.query_runs) == 3
    assert raised.value.budget.logical_calls_used == 6


@pytest.mark.anyio
async def test_stable_result_ids_do_not_depend_on_collection_time() -> None:
    first = await SerpMarketCollector(provider=FixtureProvider(), clock=lambda: NOW).collect(
        job_id=JOB_ID,
        plan=plan(),
        location_resolution_calls=0,
    )
    later = datetime(2026, 8, 28, 13, 0, tzinfo=timezone.utc)
    second = await SerpMarketCollector(provider=FixtureProvider(), clock=lambda: later).collect(
        job_id=JOB_ID,
        plan=plan(),
        location_resolution_calls=0,
    )

    assert [result.record_id for result in first.query_runs[0].results] == [
        result.record_id for result in second.query_runs[0].results
    ]


@pytest.mark.anyio
async def test_oversized_provider_error_is_not_misclassified_as_success() -> None:
    class ErrorProvider:
        async def search(self, call: SerpPlannedCall) -> SerpProviderResponse:
            if call.engine == "google_maps":
                return SerpProviderResponse({"error": "x" * 5_000})
            return SerpProviderResponse(load_fixture("empty.json"))

    with pytest.raises(SerpMarketCollectionError) as raised:
        await SerpMarketCollector(provider=ErrorProvider(), clock=lambda: NOW).collect(
            job_id=JOB_ID,
            plan=plan(),
            location_resolution_calls=0,
        )

    assert raised.value.retryable is False
    assert all(run.maps_call.status == "failed_deterministic" for run in raised.value.query_runs)
