from __future__ import annotations

from datetime import timedelta
from uuid import UUID

import fakeredis.aioredis
import pytest

from app.api.v2.models import AnalyzeRequest
from app.competitors_v22.collection_stage import (
    CheckpointedCompetitorCollectionStage,
    competitor_collection_cost_counters,
)
from app.competitors_v22.models import SharedMarketSnapshot
from app.competitors_v22.public_profile_stage import CompetitorPublicProfileResult
from app.competitors_v22.selection import analysis_discovery_input_digest
from app.competitors_v22.site_stage import CompetitorSiteInventoryResult
from app.jobs_v22.checkpoints import JobCheckpoints
from app.jobs_v22.digest import request_digest
from app.jobs_v22.errors import DeterministicJobError
from test_v22_competitor_candidates import business_records, snapshot as market_snapshot
from test_v22_competitor_models import JOB_ID, NOW, candidate, confirmed, discovery_request, discovery_result


MARKET_ID = UUID("44444444-4444-4444-8444-444444444444")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def request() -> AnalyzeRequest:
    source = discovery_request()
    return AnalyzeRequest(
        case_id=source.case_id,
        report_type="prospect",
        business_identity=source.business_identity,
        primary_service=source.primary_service,
        target_market=source.target_market,
        queries=source.queries,
        competitors=[confirmed(index) for index in range(1, 4)],
    )


def shared_market() -> SharedMarketSnapshot:
    records = []
    for index in range(1, 4):
        records.extend(
            business_records(
                f"Competitor {index}",
                f"competitor-{index}.test",
                1000 + index * 10,
            )
        )
    snapshot = market_snapshot(records).model_copy(update={"job_id": JOB_ID})
    checksum = request_digest(snapshot.model_dump(mode="json"))
    return SharedMarketSnapshot(
        schema_version="competitor_shared_market_v1",
        snapshot_id=MARKET_ID,
        source_job_id=JOB_ID,
        input_digest=analysis_discovery_input_digest(request()),
        snapshot_checksum=checksum,
        created_at=NOW,
        expires_at=NOW + timedelta(hours=24),
        snapshot=snapshot,
    )


def discovery(shared):
    return discovery_result().model_copy(
        update={
            "discovery_id": JOB_ID,
            "input_digest": shared.input_digest,
            "market_snapshot_id": shared.snapshot_id,
            "market_snapshot_checksum": shared.snapshot_checksum,
            "candidates": [candidate(index) for index in range(1, 4)],
        }
    )


class UnavailableSiteStage:
    def __init__(self) -> None:
        self.calls = 0

    async def collect(self, **kwargs):
        self.calls += 1
        return [
            CompetitorSiteInventoryResult(
                competitor_id=item.competitor_id,
                status="unavailable",
                limitations=["Website unavailable."],
            )
            for item in kwargs["competitors"]
        ]


class PublicStage:
    def __init__(self, *, traceable: bool = True) -> None:
        self.calls = 0
        self.traceable = traceable

    async def collect_one(self, **kwargs):
        self.calls += 1
        item = kwargs["competitor"]
        return CompetitorPublicProfileResult(
            competitor_id=item.competitor_id,
            profile=None,
            reviews=(),
            identity_traceable=self.traceable,
            profile_status="unavailable",
            reviews_status="unavailable",
            place_detail_calls=0,
            review_page_calls=0,
            checkpoint_hits=0,
            limitations=("Public profile unavailable.",),
        )


@pytest.mark.anyio
async def test_collection_keeps_three_competitors_when_optional_sources_are_unavailable() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    checkpoints = JobCheckpoints(redis, prefix="test:v22", ttl_seconds=604_800)
    shared = shared_market()
    sites = UnavailableSiteStage()
    profiles = PublicStage()
    stage = CheckpointedCompetitorCollectionStage(
        site_stage=sites,
        profile_stage=profiles,
        clock=lambda: NOW,
    )

    result = await stage.collect(
        job_id=JOB_ID,
        request=request(),
        discovery=discovery(shared),
        shared_market=shared,
        checkpoints=checkpoints,
    )
    repeated = await stage.collect(
        job_id=JOB_ID,
        request=request(),
        discovery=discovery(shared),
        shared_market=shared,
        checkpoints=checkpoints,
    )

    assert result == repeated
    assert len(result.competitors) == 3
    assert all(item.site_status == "unavailable" and item.analyzed_page_count == 0 for item in result.competitors)
    assert result.budget.site_discovery_limit_each == 50
    assert result.budget.site_deep_limit_each == 10
    assert result.budget.provider_attempt_limit == 15
    assert sites.calls == 1
    assert profiles.calls == 3
    assert competitor_collection_cost_counters(result)["competitor_provider_attempts"] == 0


@pytest.mark.anyio
async def test_untraceable_identity_requires_competitor_replacement() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    checkpoints = JobCheckpoints(redis, prefix="test:v22", ttl_seconds=604_800)
    shared = shared_market()
    stage = CheckpointedCompetitorCollectionStage(
        site_stage=UnavailableSiteStage(),
        profile_stage=PublicStage(traceable=False),
        clock=lambda: NOW,
    )

    with pytest.raises(DeterministicJobError) as caught:
        await stage.collect(
            job_id=JOB_ID,
            request=request(),
            discovery=discovery(shared),
            shared_market=shared,
            checkpoints=checkpoints,
        )

    assert caught.value.error_code == "V22_COMPETITOR_IDENTITY_UNTRACEABLE"
