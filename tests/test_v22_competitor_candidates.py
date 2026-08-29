from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
from uuid import UUID

from app.collectors.serp_market_models import (
    SerpCallRecord,
    SerpMarketBudget,
    SerpMarketResultRecord,
    SerpMarketSnapshot,
    SerpQueryRun,
    SerpResultCount,
    SerpTargetPoint,
)
from app.competitors_v22.candidates import rank_competitor_candidates
from app.report_v22.models import BusinessIdentity, TargetMarket


NOW = datetime(2026, 8, 29, 9, 0, tzinfo=timezone.utc)
JOB_ID = UUID("55555555-5555-4555-8555-555555555555")
QUERIES = ["emergency plumber", "plumber near me", "24 hour plumber"]


def digest(seed: int) -> str:
    return "sha256:" + hashlib.sha256(str(seed).encode()).hexdigest()


def target_market() -> TargetMarket:
    return TargetMarket(
        display_name="Austin, TX",
        country_code="US",
        region="Texas",
        city="Austin",
        postal_code="78701",
        latitude=30.2672,
        longitude=-97.7431,
    )


def client_business() -> BusinessIdentity:
    return BusinessIdentity(
        business_name="Client Plumbing",
        site_url="https://client.example",
        normalized_domain="client.example",
        operating_model="service_area",
        primary_location=target_market(),
        public_gbp_url="https://www.google.com/maps?cid=999",
    )


def call(query: str, engine: str, seed: int) -> SerpCallRecord:
    value = digest(seed)
    return SerpCallRecord(
        call_id=value,
        request_digest=value,
        query=query,
        engine=engine,
        result_types=["maps"] if engine == "google_maps" else ["local_pack", "organic"],
        latitude=30.2672,
        longitude=-97.7431,
        country_code="US",
        language="en",
        device="mobile",
        maps_zoom=14 if engine == "google_maps" else None,
        status="succeeded",
        started_at=NOW,
        completed_at=NOW,
        provider_search_id=f"search-{seed}",
        response_checksum=digest(seed + 10),
        logical_call_used=True,
        provider_attempts=1,
    )


def result(
    *,
    seed: int,
    query: str,
    result_type: str,
    position: int,
    name: str,
    url: str,
    place_id: str | None = None,
    data_id: str | None = None,
    cid: str | None = None,
    address: str | None = "100 Main St, Austin, TX",
    categories: list[str] | None = None,
    snippet: str | None = "Emergency plumbing service in Austin.",
) -> SerpMarketResultRecord:
    engine_seed = seed * 10 + (1 if result_type == "maps" else 2)
    return SerpMarketResultRecord(
        record_id=digest(seed + 30),
        call_id=digest(engine_seed),
        query=query,
        result_type=result_type,
        position=position,
        rank_source="provider_position",
        display_name=name,
        url=url,
        normalized_domain=url.split("//", 1)[1].split("/", 1)[0].casefold().removeprefix("www."),
        provider_place_id=place_id,
        provider_data_id=data_id,
        provider_cid=cid,
        address=address,
        categories=categories or ["Plumber"],
        rating=4.8,
        review_count=120,
        snippet=snippet,
        provider_search_id=f"search-{engine_seed}",
        response_checksum=digest(engine_seed + 10),
        observed_at=NOW,
        latitude=30.2672,
        longitude=-97.7431,
        country_code="US",
        language="en",
        device="mobile",
    )


def snapshot(records: list[SerpMarketResultRecord]) -> SerpMarketSnapshot:
    runs: list[SerpQueryRun] = []
    counts = Counter(item.result_type for item in records)
    for index, query in enumerate(QUERIES, start=1):
        maps_seed = index * 10 + 1
        google_seed = index * 10 + 2
        query_records = []
        for item in records:
            if item.query != query:
                continue
            expected_call = digest(maps_seed if item.result_type == "maps" else google_seed)
            query_records.append(item.model_copy(update={"call_id": expected_call}))
        runs.append(
            SerpQueryRun(
                query_index=index,
                query=query,
                maps_call=call(query, "google_maps", maps_seed),
                google_call=call(query, "google", google_seed),
                results=query_records,
                complete=True,
            )
        )
    return SerpMarketSnapshot(
        schema_version="serp_market_snapshot_v1",
        job_id=JOB_ID,
        started_at=NOW,
        completed_at=NOW,
        target_point=SerpTargetPoint(
            requested_label="Austin, TX",
            canonical_name="Austin, Texas, United States",
            country_code="US",
            latitude=30.2672,
            longitude=-97.7431,
            source="explicit_coordinates",
            resolved_at=NOW,
        ),
        device="mobile",
        language="en",
        country_code="US",
        maps_zoom=14,
        queries=QUERIES,
        query_runs=runs,
        result_counts=[
            SerpResultCount(result_type="maps", count=counts["maps"]),
            SerpResultCount(result_type="local_pack", count=counts["local_pack"]),
            SerpResultCount(result_type="organic", count=counts["organic"]),
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


def business_records(name: str, domain: str, seed: int, *, position: int = 1) -> list[SerpMarketResultRecord]:
    return [
        result(
            seed=seed + index,
            query=query,
            result_type="maps",
            position=position,
            name=name,
            url=f"https://{domain}/service",
            place_id=f"place-{seed}",
            data_id=f"data-{seed}",
            cid=str(1000 + seed),
            address=f"{seed} Main St, Austin, TX",
        )
        for index, query in enumerate(QUERIES)
    ]


def test_candidate_identity_merges_across_queries_and_result_types() -> None:
    records = business_records("Alpha Plumbing LLC", "alpha.example", 10)
    records.append(
        result(
            seed=90,
            query=QUERIES[0],
            result_type="organic",
            position=2,
            name="Alpha Plumbing | Emergency Service",
            url="https://www.alpha.example/emergency?utm_source=google",
            address=None,
        )
    )
    ranked = rank_competitor_candidates(
        snapshot(records),
        business=client_business(),
        primary_service="Emergency plumbing",
        target_market=target_market(),
    )

    assert len(ranked.candidates) == 1
    candidate = ranked.candidates[0]
    assert candidate.business_name == "Alpha Plumbing LLC"
    assert str(candidate.website_url) == "https://alpha.example/"
    assert candidate.query_appearance_count == 3
    assert candidate.best_position == 1
    assert str(candidate.public_gbp_url) == "https://www.google.com/maps?cid=1010"
    assert ranked.audit_records[0].score.query_coverage == 1


def test_same_name_different_places_stay_distinct_but_shared_site_is_suppressed() -> None:
    records = [
        result(
            seed=101,
            query=QUERIES[0],
            result_type="maps",
            position=1,
            name="Branch Plumbing",
            url="https://branch.example/",
            place_id="place-north",
            address="1 North St, Austin, TX",
        ),
        result(
            seed=102,
            query=QUERIES[1],
            result_type="maps",
            position=2,
            name="Branch Plumbing",
            url="https://branch.example/",
            place_id="place-south",
            address="2 South St, Austin, TX",
        ),
        result(
            seed=103,
            query=QUERIES[2],
            result_type="organic",
            position=3,
            name="Branch Plumbing Services",
            url="https://branch.example/services",
            address=None,
        ),
    ]
    ranked = rank_competitor_candidates(
        snapshot(records),
        business=client_business(),
        primary_service="Emergency plumbing",
        target_market=target_market(),
    )

    assert len(ranked.candidates) == 1
    assert sum(item.reason_code == "shared_competitor_website" for item in ranked.audit_records) == 1
    assert sum(item.reason_code == "ambiguous_identity" for item in ranked.audit_records) == 1


def test_client_and_platform_results_are_excluded() -> None:
    records = business_records("Client Plumbing", "client.example", 20)
    records.extend(
        [
            result(
                seed=201,
                query=QUERIES[0],
                result_type="organic",
                position=1,
                name="Best Plumbers in Austin",
                url="https://www.yelp.com/search?find_desc=plumber",
            ),
            *business_records("Real Rival Plumbing", "rival.example", 30),
        ]
    )
    ranked = rank_competitor_candidates(
        snapshot(records),
        business=client_business(),
        primary_service="Emergency plumbing",
        target_market=target_market(),
    )

    assert [item.business_name for item in ranked.candidates] == ["Real Rival Plumbing"]
    assert {item.reason_code for item in ranked.audit_records} >= {"client_business", "blocked_platform"}


def test_ranking_is_deterministic_and_manual_market_candidate_can_replace_sixth() -> None:
    records: list[SerpMarketResultRecord] = []
    for index in range(1, 8):
        records.extend(
            business_records(
                f"Rival {index} Plumbing",
                f"rival-{index}.example",
                300 + index * 10,
                position=index,
            )
        )
    market_snapshot = snapshot(records)
    first = rank_competitor_candidates(
        market_snapshot,
        business=client_business(),
        primary_service="Emergency plumbing",
        target_market=target_market(),
    )
    supplemented = rank_competitor_candidates(
        market_snapshot,
        business=client_business(),
        primary_service="Emergency plumbing",
        target_market=target_market(),
        supplemental_website_urls=["https://rival-7.example/custom"],
    )

    assert len(first.candidates) == 6
    assert [item.competitor_id for item in first.candidates] == [
        item.competitor_id
        for item in rank_competitor_candidates(
            market_snapshot,
            business=client_business(),
            primary_service="Emergency plumbing",
            target_market=target_market(),
        ).candidates
    ]
    assert "rival-7.example" not in {item.website_url.host for item in first.candidates}
    assert "rival-7.example" in {item.website_url.host for item in supplemented.candidates}


def test_fewer_than_three_candidates_returns_blocking_gap_and_unknown_supplement_is_safe() -> None:
    ranked = rank_competitor_candidates(
        snapshot(business_records("Only Rival Plumbing", "only.example", 500)),
        business=client_business(),
        primary_service="Emergency plumbing",
        target_market=target_market(),
        supplemental_website_urls=["https://not-in-market.example"],
    )

    assert ranked.ready_for_confirmation is False
    assert any(gap.gap_code == "INSUFFICIENT_COMPETITORS" and gap.blocking for gap in ranked.data_gaps)
    assert any(gap.gap_code == "SUPPLEMENTAL_COMPETITOR_NOT_IN_MARKET" for gap in ranked.data_gaps)
