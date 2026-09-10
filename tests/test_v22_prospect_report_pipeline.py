from __future__ import annotations

from uuid import UUID

import fakeredis.aioredis
import pytest

from app.competitors_v22.models import SharedMarketSnapshot
from app.jobs_v22.checkpoints import JobCheckpoints
from app.jobs_v22.digest import request_digest
from app.jobs_v22.prospect_report_pipeline import PublicProspectReportPipeline
from app.report_v22.models import TargetMarket
from findings_helpers import collection, market, site
from test_v22_competitor_collection_stage import discovery, request
from test_v22_competitor_models import NOW
from v22_copy_helpers import valid_response


JOB_ID = UUID("55555555-5555-4555-8555-555555555555")


class CopyProvider:
    model_version = "fixture-copy-v1"

    def __init__(self) -> None:
        self.calls = 0

    async def generate(self, *, job_id, request, cost_ledger=None):
        self.calls += 1
        return valid_response(request)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_pipeline_builds_and_checkpoints_a_complete_prospect_report() -> None:
    analyze = request()
    site_source = site(
        [
            {"status_code": 503, "title": "Broken"},
            {"status_code": 200, "title": "Shared", "meta_robots": ["noindex"]},
            {"status_code": 200, "title": "Shared"},
        ],
        host="example.test",
    )
    market_source = market()
    competitor_source = collection(market_source)
    analyze.business_identity.site_url = site_source.payload.root_url
    analyze.business_identity.normalized_domain = site_source.payload.canonical_host
    analyze.target_market = TargetMarket(
        display_name=market_source.payload.target_point.requested_label,
        country_code=market_source.payload.country_code,
        latitude=market_source.payload.target_point.latitude,
        longitude=market_source.payload.target_point.longitude,
    )
    analyze.business_identity.primary_location = analyze.target_market
    analyze.queries = market_source.payload.queries
    analyze.competitors = [item.competitor for item in competitor_source.payload.competitors]
    analyze.generation_limits.competitor_count = len(analyze.competitors)
    analyze = type(analyze).model_validate(analyze.model_dump(mode="python"))
    shared = SharedMarketSnapshot(
        schema_version="competitor_shared_market_v1",
        snapshot_id=market_source.binding.snapshot_id,
        source_job_id=market_source.payload.job_id,
        input_digest="sha256:" + "a" * 64,
        snapshot_checksum=request_digest(market_source.payload),
        created_at=NOW,
        expires_at=NOW.replace(day=31),
        snapshot=market_source.payload,
    )
    competitor_source.payload.market_snapshot_id = shared.snapshot_id
    competitor_source.payload.market_snapshot_checksum = shared.snapshot_checksum
    competitor_source.payload = type(competitor_source.payload).model_validate(
        competitor_source.payload.model_dump(mode="python")
    )
    provider = CopyProvider()
    evaluated_at = competitor_source.payload.completed_at
    pipeline = PublicProspectReportPipeline(
        copy_provider=provider,
        clock=lambda: evaluated_at,
    )
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    checkpoints = JobCheckpoints(redis, prefix="test:v22", ttl_seconds=604_800)
    result = discovery(shared)

    report = await pipeline.build(
        job_id=JOB_ID,
        request=analyze,
        site_inventory=site_source.payload,
        discovery=result,
        shared_market=shared,
        competitor_collection=competitor_source.payload,
        submitted_at=NOW,
        checkpoints=checkpoints,
    )
    repeated = await pipeline.build(
        job_id=JOB_ID,
        request=analyze,
        site_inventory=site_source.payload,
        discovery=result,
        shared_market=shared,
        competitor_collection=competitor_source.payload,
        submitted_at=NOW,
        checkpoints=checkpoints,
    )

    assert report == repeated
    assert report.report_version.report_id == JOB_ID
    assert report.report_version.copy_model_version == provider.model_version
    assert provider.calls == 1
