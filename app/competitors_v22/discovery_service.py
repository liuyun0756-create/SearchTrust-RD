"""Application service for market-backed v2.2 competitor discovery."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Protocol
from uuid import UUID

from app.api.v2.competitor_models import CompetitorDiscoveryRequest, CompetitorDiscoveryResult
from app.collectors.serp_market_models import SerpMarketContext, SerpMarketSnapshot
from app.competitors_v22.candidates import rank_competitor_candidates
from app.competitors_v22.market_store import SharedMarketSnapshotStore
from app.competitors_v22.normalization import normalize_domain
from app.competitors_v22.supplements import SupplementalHomepageValidator
from app.api.v2.models import DataGap
from app.jobs_v22.checkpoints import JobCheckpoints
from app.jobs_v22.cost_ledger import JobCostLedger
from app.jobs_v22.digest import request_digest


class MarketStage(Protocol):
    async def collect_context(
        self,
        *,
        job_id: UUID,
        context: SerpMarketContext,
        checkpoints: JobCheckpoints,
        cost_ledger: JobCostLedger | None = None,
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
        supplemental_validator: SupplementalHomepageValidator | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.market_stage = market_stage
        self.market_store = market_store
        self.supplemental_validator = supplemental_validator
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    async def discover(
        self,
        *,
        discovery_job_id: UUID,
        request: CompetitorDiscoveryRequest,
        checkpoints: JobCheckpoints,
        progress: Callable[[str, int, str], Awaitable[None]] | None = None,
        cost_ledger: JobCostLedger | None = None,
    ) -> CompetitorDiscoveryResult:
        now = self.clock()
        input_digest = competitor_discovery_input_digest(request)
        shared = await self.market_store.get(input_digest=input_digest, now=now)
        if shared is None:
            snapshot = await self.market_stage.collect_context(
                job_id=discovery_job_id,
                context=discovery_market_context(request),
                checkpoints=checkpoints,
                cost_ledger=cost_ledger,
            )
            shared = await self.market_store.save(
                input_digest=input_digest,
                snapshot=snapshot,
                now=now,
            )
        if progress is not None:
            await progress("ranking_candidates", 70, "Ranking competitor candidates.")
            if request.supplemental_website_urls:
                await progress(
                    "validating_supplements",
                    80,
                    "Validating supplemental competitor websites.",
                )
        supplemental_urls = [str(url) for url in request.supplemental_website_urls]
        supplemental_gaps: list[DataGap] = []
        if supplemental_urls and self.supplemental_validator is not None:
            base_ranking = rank_competitor_candidates(
                shared.snapshot,
                business=request.business_identity,
                primary_service=request.primary_service,
                target_market=request.target_market,
            )
            eligible_by_domain = {
                record.identity.normalized_domain: record
                for record in base_ranking.audit_records
                if record.disposition == "eligible" and record.candidate is not None
            }
            validated_urls: list[str] = []
            for website_url in supplemental_urls:
                record = eligible_by_domain.get(normalize_domain(website_url))
                if record is None:
                    validated_urls.append(website_url)
                    continue
                assert record.candidate is not None
                valid = await self.supplemental_validator.validate(
                    website_url=website_url,
                    expected_name=record.candidate.business_name,
                    primary_service=request.primary_service,
                    target_market=request.target_market,
                )
                if valid:
                    validated_urls.append(website_url)
                else:
                    supplemental_gaps.append(
                        DataGap(
                            gap_code="SUPPLEMENTAL_COMPETITOR_IDENTITY_UNVERIFIED",
                            message="A supplemental competitor homepage did not confirm its market identity.",
                            blocking=False,
                            resolution="Choose another market-visible competitor or run discovery again.",
                        )
                    )
            supplemental_urls = validated_urls
        ranking = rank_competitor_candidates(
            shared.snapshot,
            business=request.business_identity,
            primary_service=request.primary_service,
            target_market=request.target_market,
            supplemental_website_urls=supplemental_urls,
        )
        candidate_digest = request_digest(
            {
                "schema_version": "competitor_candidate_set_v1",
                "input_digest": input_digest,
                "supplemental_website_urls": supplemental_urls,
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
            data_gaps=[*ranking.data_gaps, *supplemental_gaps],
            limitations=limitations,
            created_at=now,
            expires_at=shared.expires_at,
        )
