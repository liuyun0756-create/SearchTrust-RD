from __future__ import annotations

from datetime import timedelta
from uuid import UUID

import fakeredis.aioredis
import pytest

from app.collectors.serp_market_models import SerpMarketContext
from app.competitors_v22.discovery_service import (
    CompetitorDiscoveryService,
    competitor_discovery_input_digest,
    discovery_market_context,
)
from app.competitors_v22.market_store import SharedMarketSnapshotStore
from app.jobs_v22.checkpoints import JobCheckpoints
from test_v22_competitor_candidates import business_records, client_business, snapshot
from test_v22_competitor_models import NOW, discovery_request


FIRST_JOB = UUID("66666666-6666-4666-8666-666666666666")
SECOND_JOB = UUID("77777777-7777-4777-8777-777777777777")


class RecordingMarketStage:
    def __init__(self, market_snapshot) -> None:
        self.market_snapshot = market_snapshot
        self.calls: list[tuple[UUID, SerpMarketContext]] = []

    async def collect_context(self, *, job_id, context, checkpoints):
        self.calls.append((job_id, context))
        return self.market_snapshot.model_copy(update={"job_id": job_id})


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def market_with_three_competitors():
    records = []
    for index, name in enumerate(("Alpha Plumbing", "Bravo Plumbing", "Charlie Plumbing"), start=1):
        records.extend(business_records(name, f"rival-{index}.example", 800 + index * 10))
    return snapshot(records)


def test_discovery_context_and_digest_are_stable_and_ignore_supplements() -> None:
    base = discovery_request()
    supplemented = discovery_request(supplemental_website_urls=["https://rival-1.example"])

    assert discovery_market_context(base) == discovery_market_context(supplemented)
    assert competitor_discovery_input_digest(base) == competitor_discovery_input_digest(supplemented)


@pytest.mark.anyio
async def test_discovery_reuses_market_snapshot_and_keeps_original_expiry() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    stage = RecordingMarketStage(market_with_three_competitors())
    shared = SharedMarketSnapshotStore(redis, prefix="test:v22", ttl_seconds=86_400)
    checkpoints = JobCheckpoints(redis, prefix="test:v22", ttl_seconds=604_800)
    service = CompetitorDiscoveryService(
        market_stage=stage,
        market_store=shared,
        clock=lambda: NOW,
    )

    first = await service.discover(
        discovery_job_id=FIRST_JOB,
        request=discovery_request(),
        checkpoints=checkpoints,
    )
    service.clock = lambda: NOW + timedelta(hours=1)
    second = await service.discover(
        discovery_job_id=SECOND_JOB,
        request=discovery_request(supplemental_website_urls=["https://rival-1.example"]),
        checkpoints=checkpoints,
    )

    assert first.ready_for_confirmation is True
    assert len(first.candidates) == 3
    assert second.market_snapshot_id == first.market_snapshot_id
    assert second.market_snapshot_checksum == first.market_snapshot_checksum
    assert second.expires_at == first.expires_at == NOW + timedelta(hours=24)
    assert len(stage.calls) == 1


@pytest.mark.anyio
async def test_empty_eligible_pool_returns_successful_blocking_result() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    stage = RecordingMarketStage(
        snapshot(business_records("Client Plumbing", "client.example", 900))
    )
    service = CompetitorDiscoveryService(
        market_stage=stage,
        market_store=SharedMarketSnapshotStore(redis, prefix="test:v22", ttl_seconds=86_400),
        clock=lambda: NOW,
    )

    result = await service.discover(
        discovery_job_id=FIRST_JOB,
        request=discovery_request(business_identity=client_business()),
        checkpoints=JobCheckpoints(redis, prefix="test:v22", ttl_seconds=604_800),
    )

    assert result.ready_for_confirmation is False
    assert result.candidates == []
    assert any(gap.gap_code == "INSUFFICIENT_COMPETITORS" for gap in result.data_gaps)


class RejectingSupplementalValidator:
    async def validate(self, **kwargs):
        return False


@pytest.mark.anyio
async def test_supplemental_candidate_requires_bounded_homepage_validation() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    records = []
    for index in range(1, 8):
        records.extend(
            business_records(
                f"Rival {index} Plumbing",
                f"rival-{index}.example",
                900 + index * 10,
                position=10 if index == 7 else 1,
            )
        )
    service = CompetitorDiscoveryService(
        market_stage=RecordingMarketStage(snapshot(records)),
        market_store=SharedMarketSnapshotStore(redis, prefix="test:v22", ttl_seconds=86_400),
        supplemental_validator=RejectingSupplementalValidator(),
        clock=lambda: NOW,
    )

    result = await service.discover(
        discovery_job_id=FIRST_JOB,
        request=discovery_request(supplemental_website_urls=["https://rival-7.example"]),
        checkpoints=JobCheckpoints(redis, prefix="test:v22", ttl_seconds=604_800),
    )

    assert "rival-7.example" not in {candidate.website_url.host for candidate in result.candidates}
    assert any(
        gap.gap_code == "SUPPLEMENTAL_COMPETITOR_IDENTITY_UNVERIFIED"
        for gap in result.data_gaps
    )
