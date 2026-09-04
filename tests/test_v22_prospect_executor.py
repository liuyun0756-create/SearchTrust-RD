from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import fakeredis.aioredis
import pytest

from app.competitors_v22.selection import AnalysisDiscoveryLink, AnalysisRequestEnvelope
from app.jobs_v22.checkpoints import JobCheckpoints
from app.jobs_v22.errors import DeterministicJobError
from app.jobs_v22.executor import ProspectV22Executor
from app.report_v22.models import ReportV22
from test_v22_competitor_collection_stage import discovery, request, shared_market
from test_v22_competitor_models import NOW


JOB_ID = UUID("55555555-5555-4555-8555-555555555555")
CONTRACT_DIR = Path(__file__).resolve().parents[1] / "contracts" / "v2.2"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def prospect_report() -> ReportV22:
    return ReportV22.model_validate_json(
        (CONTRACT_DIR / "fixtures" / "prospect.json").read_text(encoding="utf-8")
    )


class DiscoveryStore:
    def __init__(self, result) -> None:
        self.result = result

    async def require_state(self, discovery_id):
        return SimpleNamespace(status="succeeded", result=self.result)


class MarketStore:
    def __init__(self, shared) -> None:
        self.shared = shared
        self.input_digest = None

    async def get(self, *, input_digest, now):
        self.input_digest = input_digest
        return self.shared


class SiteStage:
    def __init__(self) -> None:
        self.calls = 0

    async def collect(self, **kwargs):
        self.calls += 1
        return "site-snapshot"


class CompetitorStage:
    def __init__(self) -> None:
        self.calls = 0
        self.kwargs = None

    async def collect(self, **kwargs):
        self.calls += 1
        self.kwargs = kwargs
        return "competitor-snapshot"


class ReportPipeline:
    def __init__(self, report) -> None:
        self.report = report
        self.kwargs = None

    async def build(self, **kwargs):
        self.kwargs = kwargs
        return self.report


def envelope(shared, result) -> AnalysisRequestEnvelope:
    return AnalysisRequestEnvelope(
        schema_version="v22_analysis_request_envelope_v1",
        analyze_request=request(),
        competitor_discovery=AnalysisDiscoveryLink(
            discovery_id=result.discovery_id,
            candidate_digest=result.candidate_digest,
            market_snapshot_id=shared.snapshot_id,
            market_snapshot_checksum=shared.snapshot_checksum,
        ),
    )


@pytest.mark.anyio
async def test_executor_reuses_confirmed_discovery_and_shared_market_snapshot() -> None:
    shared = shared_market()
    result = discovery(shared)
    site_stage = SiteStage()
    competitor_stage = CompetitorStage()
    report_pipeline = ReportPipeline(prospect_report())
    market_store = MarketStore(shared)
    executor = ProspectV22Executor(
        discovery_store=DiscoveryStore(result),
        market_store=market_store,
        site_stage=site_stage,
        competitor_stage=competitor_stage,
        report_pipeline=report_pipeline,
        clock=lambda: NOW,
    )
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    checkpoints = JobCheckpoints(redis, prefix="test:v22", ttl_seconds=604_800)

    report = await executor.execute(
        job_id=JOB_ID,
        request=envelope(shared, result),
        submitted_at=NOW,
        checkpoints=checkpoints,
    )

    assert report == prospect_report()
    assert market_store.input_digest == result.input_digest
    assert site_stage.calls == 1
    assert competitor_stage.calls == 1
    assert competitor_stage.kwargs["discovery"] == result
    assert competitor_stage.kwargs["shared_market"] == shared
    assert report_pipeline.kwargs["site_inventory"] == "site-snapshot"
    assert report_pipeline.kwargs["competitor_collection"] == "competitor-snapshot"


@pytest.mark.anyio
async def test_executor_rejects_discovery_link_that_no_longer_matches_result() -> None:
    shared = shared_market()
    result = discovery(shared)
    site_stage = SiteStage()
    executor = ProspectV22Executor(
        discovery_store=DiscoveryStore(result),
        market_store=MarketStore(shared),
        site_stage=site_stage,
        competitor_stage=CompetitorStage(),
        report_pipeline=ReportPipeline(prospect_report()),
        clock=lambda: NOW,
    )
    stale = envelope(shared, result).model_copy(
        update={
            "competitor_discovery": envelope(shared, result).competitor_discovery.model_copy(
                update={"candidate_digest": "sha256:" + "c" * 64}
            )
        }
    )
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    checkpoints = JobCheckpoints(redis, prefix="test:v22", ttl_seconds=604_800)

    with pytest.raises(DeterministicJobError) as caught:
        await executor.execute(
            job_id=JOB_ID,
            request=stale,
            submitted_at=NOW,
            checkpoints=checkpoints,
        )

    assert caught.value.error_code == "V22_COMPETITOR_DISCOVERY_INVALID"
    assert site_stage.calls == 0
