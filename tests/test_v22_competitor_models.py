from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.api.v2.competitor_models import (
    CompetitorDiscoveryError,
    CompetitorDiscoveryRequest,
    CompetitorDiscoveryResult,
    CompetitorDiscoveryStatusResponse,
)
from app.api.v2.models import ApiV2ContractBundle, CompetitorCandidate, ConfirmedCompetitor, DataGap
from app.competitors_v22.models import (
    COMPETITOR_COUNT,
    COMPETITOR_DISCOVERY_CANDIDATE_LIMIT,
    COMPETITOR_DISCOVERY_SUPPLEMENTAL_LIMIT,
    COMPETITOR_PROVIDER_ATTEMPT_LIMIT,
    COMPETITOR_REVIEW_PAGE_LIMIT,
    COMPETITOR_REVIEW_SAMPLE_LIMIT,
    COMPETITOR_SITE_DEEP_LIMIT,
    COMPETITOR_SITE_DISCOVERY_LIMIT,
    CandidateScore,
    CompetitorCollectionBudget,
    CompetitorCollectionSnapshot,
    CompetitorSnapshot,
    PublicReviewRecord,
)


NOW = datetime(2026, 8, 29, 8, 0, tzinfo=timezone.utc)
CASE_ID = UUID("11111111-1111-4111-8111-111111111111")
DISCOVERY_ID = UUID("22222222-2222-4222-8222-222222222222")
JOB_ID = UUID("33333333-3333-4333-8333-333333333333")
MARKET_ID = UUID("44444444-4444-4444-8444-444444444444")
DIGEST = "sha256:" + "a" * 64
CHECKSUM = "sha256:" + "b" * 64


def business() -> dict:
    return {
        "business_name": "Example Plumbing",
        "site_url": "https://example.test",
        "normalized_domain": "example.test",
        "operating_model": "service_area",
        "primary_location": market(),
        "public_gbp_url": "https://maps.google.com/?cid=123",
    }


def market() -> dict:
    return {
        "display_name": "Austin, TX",
        "country_code": "US",
        "region": "Texas",
        "city": "Austin",
        "postal_code": "78701",
        "latitude": 30.2672,
        "longitude": -97.7431,
    }


def discovery_request(**overrides: object) -> CompetitorDiscoveryRequest:
    payload: dict[str, object] = {
        "case_id": CASE_ID,
        "business_identity": business(),
        "primary_service": "Emergency plumbing",
        "target_market": market(),
        "queries": ["emergency plumber", "plumber near me", "24 hour plumber"],
        "search_language": "en",
        "search_device": "mobile",
        "supplemental_website_urls": [],
    }
    payload.update(overrides)
    return CompetitorDiscoveryRequest.model_validate(payload)


def candidate(index: int) -> CompetitorCandidate:
    return CompetitorCandidate(
        competitor_id=f"cp_competitor_{index}",
        business_name=f"Competitor {index}",
        website_url=f"https://competitor-{index}.test",
        public_gbp_url=f"https://maps.google.com/?cid={index}",
        query_appearance_count=3,
        best_position=index,
        relevance_reason="Appears for the confirmed service queries in the target market.",
        confidence="high",
    )


def discovery_result(*, count: int = 3, ready: bool = True) -> CompetitorDiscoveryResult:
    gaps = []
    if not ready:
        gaps = [
            DataGap(
                gap_code="INSUFFICIENT_COMPETITORS",
                message="Fewer than three eligible competitors were found.",
                blocking=True,
                resolution="Add a market-visible competitor or adjust the confirmed queries.",
            )
        ]
    return CompetitorDiscoveryResult(
        discovery_id=DISCOVERY_ID,
        case_id=CASE_ID,
        input_digest=DIGEST,
        candidate_digest=CHECKSUM,
        market_snapshot_id=MARKET_ID,
        market_snapshot_checksum=CHECKSUM,
        candidates=[candidate(index) for index in range(1, count + 1)],
        ready_for_confirmation=ready,
        data_gaps=gaps,
        limitations=[],
        created_at=NOW,
        expires_at=NOW + timedelta(hours=24),
    )


def confirmed(index: int) -> ConfirmedCompetitor:
    item = candidate(index)
    return ConfirmedCompetitor(
        competitor_id=item.competitor_id,
        business_name=item.business_name,
        website_url=item.website_url,
        public_gbp_url=item.public_gbp_url,
        confirmation_source="user",
    )


def competitor_snapshot(index: int) -> CompetitorSnapshot:
    return CompetitorSnapshot(
        competitor=confirmed(index),
        query_appearance_count=3,
        best_position=index,
        analyzed_page_count=0,
        site_status="unavailable",
        public_gbp_status="unavailable",
        reviews_status="unavailable",
        site_inventory=None,
        public_gbp=None,
        reviews=[],
        limitations=["competitor_site_unavailable"],
    )


def test_discovery_request_is_strict_and_bounded() -> None:
    request = discovery_request()

    assert request.case_id == CASE_ID
    assert request.search_language == "en"
    assert request.search_device == "mobile"
    assert len(request.queries) == 3

    with pytest.raises(ValidationError, match="queries must be unique"):
        discovery_request(queries=["Plumber", "plumber", "another"])
    with pytest.raises(ValidationError):
        discovery_request(
            supplemental_website_urls=[
                "https://one.test",
                "https://two.test",
                "https://three.test",
                "https://four.test",
            ]
        )
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        discovery_request(access_token="must-not-cross")


def test_discovery_result_requires_three_candidates_when_ready() -> None:
    result = discovery_result()

    assert result.ready_for_confirmation is True
    assert len(result.candidates) == 3

    with pytest.raises(ValidationError, match="at least three candidates"):
        discovery_result(count=2, ready=True)
    with pytest.raises(ValidationError, match="blocking gap"):
        CompetitorDiscoveryResult.model_validate(
            {**discovery_result(count=3, ready=False).model_dump(), "data_gaps": []}
        )


def test_discovery_status_enforces_terminal_payloads() -> None:
    succeeded = CompetitorDiscoveryStatusResponse(
        discovery_job_id=JOB_ID,
        status="succeeded",
        stage="completed",
        progress=100,
        message="Competitor discovery complete.",
        result=discovery_result(),
        error=None,
        created_at=NOW,
        updated_at=NOW,
    )
    assert succeeded.result is not None

    error = CompetitorDiscoveryError(
        error_code="SERP_PROVIDER_UNAVAILABLE",
        user_message="Market discovery is temporarily unavailable.",
        retryable=True,
        stage="failed",
        diagnostic_id=UUID("55555555-5555-4555-8555-555555555555"),
    )
    with pytest.raises(ValidationError, match="failed discovery jobs require an error"):
        CompetitorDiscoveryStatusResponse(
            discovery_job_id=JOB_ID,
            status="failed",
            stage="failed",
            progress=30,
            message=error.user_message,
            result=None,
            error=None,
            created_at=NOW,
            updated_at=NOW,
        )


def test_candidate_score_requires_confirmed_formula() -> None:
    score = CandidateScore(
        query_coverage=0.8,
        rank=0.5,
        business_relevance=0.75,
        identity_confidence=1.0,
        total=0.71,
    )
    assert score.total == pytest.approx(0.71)

    with pytest.raises(ValidationError, match="weighted candidate formula"):
        CandidateScore(
            query_coverage=0.8,
            rank=0.5,
            business_relevance=0.75,
            identity_confidence=1.0,
            total=0.9,
        )


def test_review_model_rejects_personal_profile_fields() -> None:
    review = PublicReviewRecord(
        review_record_id="rv_" + "a" * 32,
        provider_review_id="provider-review-1",
        rating=5,
        iso_date=NOW,
        original_date_text="a day ago",
        text="Fast and helpful service.",
        owner_response_text="Thank you.",
        owner_response_iso_date=NOW,
        source="google_maps_reviews",
        collected_at=NOW,
        request_record_id="req_" + "b" * 32,
    )
    assert review.rating == 5

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        PublicReviewRecord.model_validate(
            {**review.model_dump(), "contributor_id": "private-profile-id"}
        )


def test_collection_snapshot_requires_three_unique_competitors_and_budget() -> None:
    snapshot = CompetitorCollectionSnapshot(
        schema_version="competitor_collection_snapshot_v1",
        job_id=JOB_ID,
        discovery_id=DISCOVERY_ID,
        candidate_digest=CHECKSUM,
        market_snapshot_id=MARKET_ID,
        market_snapshot_checksum=CHECKSUM,
        started_at=NOW,
        completed_at=NOW,
        competitors=[competitor_snapshot(index) for index in range(1, 4)],
        budget=CompetitorCollectionBudget(
            site_discovery_limit_each=50,
            site_deep_limit_each=10,
            review_sample_limit_each=30,
            provider_attempt_limit=15,
            provider_attempts_used=0,
            place_detail_calls=0,
            review_page_calls=0,
            checkpoint_hits=0,
            truncated=False,
        ),
        limitations=[],
    )
    assert len(snapshot.competitors) == 3

    with pytest.raises(ValidationError, match="competitor IDs must be unique"):
        CompetitorCollectionSnapshot.model_validate(
            {**snapshot.model_dump(), "competitors": [competitor_snapshot(1).model_dump()] * 3}
        )


def test_hard_limits_match_approved_design() -> None:
    assert (
        COMPETITOR_DISCOVERY_CANDIDATE_LIMIT,
        COMPETITOR_DISCOVERY_SUPPLEMENTAL_LIMIT,
        COMPETITOR_COUNT,
        COMPETITOR_SITE_DISCOVERY_LIMIT,
        COMPETITOR_SITE_DEEP_LIMIT,
        COMPETITOR_REVIEW_PAGE_LIMIT,
        COMPETITOR_REVIEW_SAMPLE_LIMIT,
        COMPETITOR_PROVIDER_ATTEMPT_LIMIT,
    ) == (6, 3, 3, 50, 10, 4, 30, 15)


def test_frozen_api_bundle_does_not_change_for_internal_discovery_contracts() -> None:
    definitions = ApiV2ContractBundle.model_json_schema(mode="serialization").get("$defs", {})

    assert "CompetitorDiscoveryRequest" not in definitions
    assert "CompetitorDiscoveryResult" not in definitions
