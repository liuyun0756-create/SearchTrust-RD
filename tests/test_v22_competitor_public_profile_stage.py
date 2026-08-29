from __future__ import annotations

from uuid import UUID

import fakeredis.aioredis
import pytest

from app.api.v2.models import ConfirmedCompetitor
from app.competitors_v22.public_profile_stage import (
    CheckpointedPublicProfileStage,
    ProviderBudgetExhausted,
    SharedProviderAttemptBudget,
)
from app.jobs_v22.checkpoints import JobCheckpoints
from test_v22_competitor_candidates import business_records, snapshot
from test_v22_competitor_models import NOW


JOB_ID = UUID("55555555-5555-4555-8555-555555555555")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def competitor() -> ConfirmedCompetitor:
    return ConfirmedCompetitor(
        competitor_id="cp_competitor_1",
        business_name="Alpha Plumbing",
        website_url="https://alpha.example/",
        public_gbp_url="https://www.google.com/maps?cid=1910",
        confirmation_source="user",
    )


def complete_market():
    records = [
        record.model_copy(update={"rating": 4.8, "review_count": 42})
        for record in business_records("Alpha Plumbing", "alpha.example", 910)
    ]
    return snapshot(records)


class RecordingProvider:
    def __init__(self) -> None:
        self.calls = []

    async def request(self, params, *, before_attempt):
        await before_attempt(1, "fingerprint")
        self.calls.append(params)
        if params["engine"] == "google_maps_reviews":
            if "next_page_token" not in params:
                return {
                    "reviews": [{"review_id": "one", "rating": 5, "iso_date": NOW.isoformat(), "snippet": "Great"}],
                    "serpapi_pagination": {"next_page_token": "page-2"},
                }
            return {
                "reviews": [{"review_id": "two", "rating": 4, "iso_date": NOW.isoformat(), "snippet": "Good"}]
            }
        raise AssertionError(params)


@pytest.mark.anyio
async def test_complete_market_profile_skips_detail_and_pages_reviews_with_checkpoints() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    checkpoints = JobCheckpoints(redis, prefix="test:v22", ttl_seconds=604_800)
    budget = SharedProviderAttemptBudget(checkpoints, JOB_ID)
    provider = RecordingProvider()
    stage = CheckpointedPublicProfileStage(provider, clock=lambda: NOW)

    first = await stage.collect_one(
        job_id=JOB_ID,
        competitor=competitor(),
        market_snapshot=complete_market(),
        language="en",
        checkpoints=checkpoints,
        budget=budget,
    )
    repeated = await stage.collect_one(
        job_id=JOB_ID,
        competitor=competitor(),
        market_snapshot=complete_market(),
        language="en",
        checkpoints=checkpoints,
        budget=budget,
    )

    assert first.profile is not None
    assert first.place_detail_calls == 0
    assert len(first.reviews) == 2
    assert all(call["engine"] == "google_maps_reviews" for call in provider.calls)
    assert all(call["sort_by"] == "newestFirst" for call in provider.calls)
    assert len(provider.calls) == 2
    assert await budget.count() == 2
    assert repeated.checkpoint_hits == 2


@pytest.mark.anyio
async def test_provider_attempt_budget_never_allows_sixteenth_attempt_and_survives_rebuild() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    checkpoints = JobCheckpoints(redis, prefix="test:v22", ttl_seconds=604_800)
    budget = SharedProviderAttemptBudget(checkpoints, JOB_ID)
    for index in range(15):
        await budget.claim(index, f"fingerprint-{index}")

    rebuilt = SharedProviderAttemptBudget(checkpoints, JOB_ID)
    with pytest.raises(ProviderBudgetExhausted):
        await rebuilt.claim(16, "last")
    assert await rebuilt.count() == 15
