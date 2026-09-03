from __future__ import annotations

from datetime import timedelta
from uuid import UUID

import fakeredis.aioredis
import pytest

from app.api.v2.models import AnalyzeRequest, ConfirmedCompetitor
from app.competitors_v22.discovery_service import CompetitorDiscoveryService
from app.competitors_v22.market_store import SharedMarketSnapshotStore
from app.competitors_v22.selection import DiscoverySelectionError, RedisDiscoveryVerifier
from app.competitors_v22.store import CompetitorDiscoveryStore
from app.competitors_v22.supplements import SupplementalHomepageValidator
from app.jobs_v22.checkpoints import JobCheckpoints
from app.preflight_v22.fetcher import HomepageSnapshot
from test_v22_competitor_candidates import business_records, snapshot
from test_v22_competitor_discovery_service import RecordingMarketStage
from test_v22_competitor_models import JOB_ID, NOW, discovery_request


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def market_snapshot():
    records = []
    for index, name in enumerate(("Alpha Plumbing", "Bravo Plumbing", "Charlie Plumbing"), start=1):
        records.extend(business_records(name, f"rival-{index}.example", 800 + index * 10))
    return snapshot(records)


def analyze_request(source, candidates, *, count: int = 3) -> AnalyzeRequest:
    return AnalyzeRequest(
        case_id=source.case_id,
        report_type="prospect",
        business_identity=source.business_identity,
        primary_service=source.primary_service,
        target_market=source.target_market,
        queries=source.queries,
        competitors=[
            ConfirmedCompetitor(
                competitor_id=candidate.competitor_id,
                business_name=candidate.business_name,
                website_url=candidate.website_url,
                public_gbp_url=candidate.public_gbp_url,
                confirmation_source="user",
            )
            for candidate in candidates[:count]
        ],
        generation_limits={"competitor_count": count},
    )


async def setup_verifier():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    request = discovery_request()
    market_store = SharedMarketSnapshotStore(redis, prefix="test:v22", ttl_seconds=86_400)
    service = CompetitorDiscoveryService(
        market_stage=RecordingMarketStage(market_snapshot()),
        market_store=market_store,
        clock=lambda: NOW,
    )
    result = await service.discover(
        discovery_job_id=JOB_ID,
        request=request,
        checkpoints=JobCheckpoints(redis, prefix="test:v22", ttl_seconds=604_800),
    )
    store = CompetitorDiscoveryStore(redis, prefix="test:v22", state_ttl_seconds=604_800)
    await store.register_job(
        discovery_job_id=JOB_ID,
        case_id=request.case_id,
        idempotency_key="discovery-selection-1",
        request_payload=request.model_dump(mode="json"),
        now=NOW,
    )
    await store.transition(
        JOB_ID,
        status="running",
        stage="collecting_market",
        progress=1,
        message="Collecting.",
        now=NOW,
        attempt_count=1,
    )
    await store.transition(
        JOB_ID,
        status="succeeded",
        stage="completed",
        progress=100,
        message="Complete.",
        now=NOW,
        result=result,
    )
    return RedisDiscoveryVerifier(store=store, market_store=market_store), request, result


@pytest.mark.anyio
async def test_valid_user_selection_links_frozen_discovery_snapshot() -> None:
    verifier, discovery, result = await setup_verifier()

    link = await verifier.verify(
        discovery_id=JOB_ID,
        request=analyze_request(discovery, result.candidates),
        now=NOW + timedelta(hours=1),
    )

    assert link.discovery_id == JOB_ID
    assert link.candidate_digest == result.candidate_digest
    assert link.market_snapshot_checksum == result.market_snapshot_checksum


@pytest.mark.anyio
@pytest.mark.parametrize("competitor_count", [1, 2, 3])
async def test_user_can_confirm_one_to_three_discovered_competitors(
    competitor_count: int,
) -> None:
    verifier, discovery, result = await setup_verifier()

    link = await verifier.verify(
        discovery_id=JOB_ID,
        request=analyze_request(discovery, result.candidates, count=competitor_count),
        now=NOW + timedelta(hours=1),
    )

    assert link.discovery_id == JOB_ID


@pytest.mark.anyio
async def test_expired_discovery_is_rejected() -> None:
    verifier, discovery, result = await setup_verifier()

    with pytest.raises(DiscoverySelectionError) as caught:
        await verifier.verify(
            discovery_id=JOB_ID,
            request=analyze_request(discovery, result.candidates),
            now=NOW + timedelta(hours=25),
        )

    assert caught.value.error_code == "COMPETITOR_DISCOVERY_EXPIRED"


@pytest.mark.anyio
async def test_tampered_or_system_confirmed_candidate_is_rejected() -> None:
    verifier, discovery, result = await setup_verifier()
    request = analyze_request(discovery, result.candidates)
    changed = request.competitors[0].model_copy(update={"business_name": "Changed Name"})
    request = request.model_copy(update={"competitors": [changed, *request.competitors[1:]]})

    with pytest.raises(DiscoverySelectionError) as caught:
        await verifier.verify(discovery_id=JOB_ID, request=request, now=NOW)

    assert caught.value.error_code == "COMPETITOR_SELECTION_TAMPERED"

    system = request.competitors[0].model_copy(
        update={"business_name": result.candidates[0].business_name, "confirmation_source": "system"}
    )
    request = request.model_copy(update={"competitors": [system, *request.competitors[1:]]})
    with pytest.raises(DiscoverySelectionError) as caught:
        await verifier.verify(discovery_id=JOB_ID, request=request, now=NOW)
    assert caught.value.error_code == "COMPETITOR_CONFIRMATION_REQUIRED"


class StaticHomepageFetcher:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    async def fetch(self, website_url):
        return self.snapshot


@pytest.mark.anyio
async def test_supplemental_homepage_requires_identity_or_service_market_evidence() -> None:
    html = """
    <script type="application/ld+json">
    {"@type":"Plumber","name":"Alpha Plumbing","serviceType":"Emergency Plumbing",
     "areaServed":"Austin","address":{"addressLocality":"Austin","addressRegion":"TX",
     "addressCountry":"US"}}
    </script>
    """
    validator = SupplementalHomepageValidator(
        StaticHomepageFetcher(
            HomepageSnapshot(
                source_url="https://rival-1.example/",
                normalized_site_url="https://rival-1.example/",
                status_code=200,
                html=html,
            )
        )
    )

    assert await validator.validate(
        website_url="https://rival-1.example/",
        expected_name="Alpha Plumbing",
        primary_service="Emergency Plumbing",
        target_market=discovery_request().target_market,
    )
    assert not await validator.validate(
        website_url="https://different.example/",
        expected_name="Alpha Plumbing",
        primary_service="Emergency Plumbing",
        target_market=discovery_request().target_market,
    )
