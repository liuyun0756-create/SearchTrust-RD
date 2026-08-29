"""Application service for market-backed v2.2 competitor discovery."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Protocol
from uuid import UUID

from app.api.v2.competitor_models import CompetitorDiscoveryRequest, CompetitorDiscoveryResult
from app.collectors.serp_market_models import SerpMarketContext, SerpMarketSnapshot
from app.competitors_v22.candidates import rank_competitor_candidates
from app.competitors_v22.market_store import SharedMarketSnapshotStore
from app.jobs_v22.checkpoints import JobCheckpoints
from app.jobs_v22.digest import request_digest


class MarketStage(Protocol):
    async def collect_context(
        self,
        *,
        job_id: UUID,
        context: SerpMarketContext,
        checkpoints: JobCheckpoints,
    ) -> SerpMarketSnapshot: ...


def discovery_market_context(request: CompetitorDiscoveryRequest) -> SerpMarketContext:
    return SerpMarketContext(
        target_market=request.target_market,
        queries=request.queries,
        device=request.search_device,
        language=request.search_language,
    )


def competitor_discovery_input_digest(request: CompetitorDiscoveryRequest) -> str:
    return request_digest(
        {
            "schema_version": "competitor_discovery_input_v1",
            "case_id": str(request.case_id),
            "business_identity": request.business_identity.model_dump(mode="json"),
            "primary_service": request.primary_service,
            "market_context": discovery_market_context(request).model_dump(mode="json"),
        }
    )


class CompetitorDiscoveryService:
    def __init__(
        self,
        *,
        market_stage: MarketStage,
        market_store: SharedMarketSnapshotStore,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.market_stage = market_stage
        self.market_store = market_store
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    async def discover(
        self,
        *,
        discovery_job_id: UUID,
        request: CompetitorDiscoveryRequest,
        checkpoints: JobCheckpoints,
    ) -> CompetitorDiscoveryResult:
        now = self.clock()
        input_digest = competitor_discovery_input_digest(request)
        shared = await self.market_store.get(input_digest=input_digest, now=now)
        if shared is None:
            snapshot = await self.market_stage.collect_context(
                job_id=discovery_job_id,
                context=discovery_market_context(request),
                checkpoints=checkpoints,
            )
            shared = await self.market_store.save(
                input_digest=input_digest,
                snapshot=snapshot,
                now=now,
            )
        ranking = rank_competitor_candidates(
            shared.snapshot,
            business=request.business_identity,
            primary_service=request.primary_service,
            target_market=request.target_market,
            supplemental_website_urls=[str(url) for url in request.supplemental_website_urls],
        )
        candidate_digest = request_digest(
            {
                "schema_version": "competitor_candidate_set_v1",
                "input_digest": input_digest,
                "supplemental_website_urls": [str(url) for url in request.supplemental_website_urls],
                "ranking": ranking.model_dump(mode="json"),
            }
        )
        limitations = list(dict.fromkeys([*shared.snapshot.limitations, *ranking.limitations]))
        return CompetitorDiscoveryResult(
            discovery_id=discovery_job_id,
            case_id=request.case_id,
            input_digest=input_digest,
            candidate_digest=candidate_digest,
            market_snapshot_id=shared.snapshot_id,
            market_snapshot_checksum=shared.snapshot_checksum,
            candidates=ranking.candidates,
            ready_for_confirmation=ranking.ready_for_confirmation,
            data_gaps=ranking.data_gaps,
            limitations=limitations,
            created_at=now,
            expires_at=shared.expires_at,
        )
