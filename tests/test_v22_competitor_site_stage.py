from __future__ import annotations

from uuid import UUID

import fakeredis.aioredis
import pytest

from app.competitors_v22.site_stage import CheckpointedCompetitorSiteStage
from app.jobs_v22.checkpoints import JobCheckpoints
from test_v22_competitor_models import confirmed, discovery_request
from test_v22_site_inventory_models import snapshot


JOB_ID = UUID("55555555-5555-4555-8555-555555555555")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class RecordingSiteCollector:
    def __init__(self, *, fail_id: str | None = None) -> None:
        self.calls = []
        self.fail_id = fail_id

    async def collect_inventory_for_site(self, **kwargs):
        self.calls.append(kwargs)
        competitor_id = kwargs["checkpoint_namespace"].split(":", 1)[1]
        if competitor_id == self.fail_id:
            raise OSError("unavailable")
        domain = kwargs["site_url"].split("//", 1)[1].strip("/")
        return snapshot(
            root_url=kwargs["site_url"],
            canonical_host=domain,
            discovery_limit=kwargs["discovery_limit"],
            deep_analysis_limit=kwargs["deep_analysis_limit"],
        )


@pytest.mark.anyio
async def test_competitor_sites_use_independent_50_10_budgets_without_gsc() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    checkpoints = JobCheckpoints(redis, prefix="test:v22", ttl_seconds=604_800)
    collector = RecordingSiteCollector()
    stage = CheckpointedCompetitorSiteStage(collector)
    competitors = [confirmed(index) for index in range(1, 4)]

    results = await stage.collect(
        job_id=JOB_ID,
        competitors=competitors,
        primary_service="Emergency plumbing",
        target_market=discovery_request().target_market,
        max_pages_each=25,
        checkpoints=checkpoints,
    )

    assert [result.status for result in results] == ["available"] * 3
    assert {call["discovery_limit"] for call in collector.calls} == {50}
    assert {call["deep_analysis_limit"] for call in collector.calls} == {10}
    assert all(call["gsc_priorities"] == [] for call in collector.calls)
    assert len({call["checkpoint_namespace"] for call in collector.calls}) == 3


@pytest.mark.anyio
async def test_one_inaccessible_competitor_does_not_abort_other_sites() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    checkpoints = JobCheckpoints(redis, prefix="test:v22", ttl_seconds=604_800)
    competitors = [confirmed(index) for index in range(1, 4)]
    collector = RecordingSiteCollector(fail_id=competitors[1].competitor_id)

    results = await CheckpointedCompetitorSiteStage(collector).collect(
        job_id=JOB_ID,
        competitors=competitors,
        primary_service="Emergency plumbing",
        target_market=discovery_request().target_market,
        max_pages_each=10,
        checkpoints=checkpoints,
    )

    assert [result.status for result in results] == ["available", "unavailable", "available"]
    assert results[1].inventory is None
    assert results[1].limitations
