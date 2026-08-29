"""Final checkpointed aggregation boundary for v2.2 competitor collection."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol
from uuid import UUID

from pydantic import ValidationError

from app.api.v2.competitor_models import CompetitorDiscoveryResult
from app.api.v2.models import AnalyzeRequest
from app.competitors_v22.models import (
    COMPETITOR_PROVIDER_ATTEMPT_LIMIT,
    COMPETITOR_REVIEW_SAMPLE_LIMIT,
    COMPETITOR_SITE_DEEP_LIMIT,
    COMPETITOR_SITE_DISCOVERY_LIMIT,
    CompetitorCollectionBudget,
    CompetitorCollectionSnapshot,
    CompetitorSnapshot,
    SharedMarketSnapshot,
)
from app.competitors_v22.public_profile_stage import (
    CompetitorPublicProfileResult,
    SharedProviderAttemptBudget,
)
from app.competitors_v22.selection import (
    analysis_discovery_input_digest,
    validate_confirmed_competitors,
)
from app.competitors_v22.site_stage import (
    CompetitorSiteInventoryResult,
)
from app.jobs_v22.checkpoints import JobCheckpoints
from app.jobs_v22.digest import canonical_json_bytes, request_digest
from app.jobs_v22.errors import DeterministicJobError


class CompetitorCollectionCheckpointError(DeterministicJobError):
    def __init__(self) -> None:
        super().__init__(
            "V22_COMPETITOR_COLLECTION_CHECKPOINT_INVALID",
            "A saved competitor collection snapshot could not be validated.",
        )


class SiteStage(Protocol):
    async def collect(self, **kwargs) -> list[CompetitorSiteInventoryResult]: ...


class ProfileStage(Protocol):
    async def collect_one(self, **kwargs) -> CompetitorPublicProfileResult: ...


class CheckpointedCompetitorCollectionStage:
    def __init__(self, *, site_stage: SiteStage, profile_stage: ProfileStage, clock=None) -> None:
        self.site_stage = site_stage
        self.profile_stage = profile_stage
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    async def collect(
        self,
        *,
        job_id: UUID,
        request: AnalyzeRequest,
        discovery: CompetitorDiscoveryResult,
        shared_market: SharedMarketSnapshot,
        checkpoints: JobCheckpoints,
    ) -> CompetitorCollectionSnapshot:
        now = self.clock()
        if (
            discovery.expires_at <= now
            or discovery.case_id != request.case_id
            or discovery.input_digest != analysis_discovery_input_digest(request)
        ):
            raise DeterministicJobError(
                "V22_COMPETITOR_DISCOVERY_INVALID",
                "The competitor discovery is expired or belongs to another case.",
            )
        if (
            shared_market.snapshot_id != discovery.market_snapshot_id
            or shared_market.snapshot_checksum != discovery.market_snapshot_checksum
            or shared_market.input_digest != discovery.input_digest
            or request_digest(shared_market.snapshot.model_dump(mode="json"))
            != shared_market.snapshot_checksum
        ):
            raise DeterministicJobError(
                "V22_COMPETITOR_MARKET_SNAPSHOT_INVALID",
                "The competitor market snapshot does not match the confirmed discovery.",
            )
        validate_confirmed_competitors(request=request, candidates=discovery.candidates)
        identity = {
            "schema_version": "competitor_collection_input_v1",
            "job_id": str(job_id),
            "request": request.model_dump(mode="json"),
            "discovery_id": str(discovery.discovery_id),
            "candidate_digest": discovery.candidate_digest,
            "market_snapshot_checksum": shared_market.snapshot_checksum,
        }
        digest = request_digest(identity)
        final_key = f"competitor-collection:snapshot:{digest[7:]}"

        async def operation():
            started = self.clock()
            site_results = await self.site_stage.collect(
                job_id=job_id,
                competitors=request.competitors,
                primary_service=request.primary_service,
                target_market=request.target_market,
                max_pages_each=request.generation_limits.max_competitor_pages_each,
                checkpoints=checkpoints,
            )
            site_by_id = {item.competitor_id: item for item in site_results}
            expected_ids = {item.competitor_id for item in request.competitors}
            if set(site_by_id) != expected_ids:
                raise DeterministicJobError(
                    "V22_COMPETITOR_SOURCE_IDENTITY_MISMATCH",
                    "Competitor website results did not match the confirmed identities.",
                )
            budget = SharedProviderAttemptBudget(checkpoints, job_id)
            profiles: list[CompetitorPublicProfileResult] = []
            for competitor in request.competitors:
                profiles.append(
                    await self.profile_stage.collect_one(
                        job_id=job_id,
                        competitor=competitor,
                        market_snapshot=shared_market.snapshot,
                        language=shared_market.snapshot.language,
                        checkpoints=checkpoints,
                        budget=budget,
                    )
                )
            profile_by_id = {item.competitor_id: item for item in profiles}
            if set(profile_by_id) != expected_ids:
                raise DeterministicJobError(
                    "V22_COMPETITOR_SOURCE_IDENTITY_MISMATCH",
                    "Public competitor results did not match the confirmed identities.",
                )
            if any(
                not profile.identity_traceable
                and site_by_id[profile.competitor_id].inventory is None
                for profile in profiles
            ):
                raise DeterministicJobError(
                    "V22_COMPETITOR_IDENTITY_UNTRACEABLE",
                    "A confirmed competitor identity could not be traced. Replace that competitor.",
                )
            candidate_by_id = {item.competitor_id: item for item in discovery.candidates}
            competitors: list[CompetitorSnapshot] = []
            limitations: list[str] = []
            for competitor in request.competitors:
                candidate = candidate_by_id[competitor.competitor_id]
                site = site_by_id[competitor.competitor_id]
                public = profile_by_id[competitor.competitor_id]
                item_limitations = list(dict.fromkeys([*site.limitations, *public.limitations]))
                limitations.extend(item_limitations)
                competitors.append(
                    CompetitorSnapshot(
                        competitor=competitor,
                        query_appearance_count=candidate.query_appearance_count,
                        best_position=candidate.best_position,
                        analyzed_page_count=(site.inventory.deep_analyzed_count if site.inventory else 0),
                        site_status=site.status,
                        public_gbp_status=public.profile_status,
                        reviews_status=public.reviews_status,
                        site_inventory=site.inventory,
                        public_gbp=public.profile,
                        reviews=list(public.reviews),
                        limitations=item_limitations,
                    )
                )
            attempts = await budget.count()
            detail_calls = sum(item.place_detail_calls for item in profiles)
            review_calls = sum(item.review_page_calls for item in profiles)
            checkpoint_hits = sum(item.checkpoint_hits for item in profiles)
            truncated = attempts >= COMPETITOR_PROVIDER_ATTEMPT_LIMIT or any(
                "budget" in limitation.casefold() for limitation in limitations
            )
            result = CompetitorCollectionSnapshot(
                schema_version="competitor_collection_snapshot_v1",
                job_id=job_id,
                discovery_id=discovery.discovery_id,
                candidate_digest=discovery.candidate_digest,
                market_snapshot_id=shared_market.snapshot_id,
                market_snapshot_checksum=shared_market.snapshot_checksum,
                started_at=started,
                completed_at=self.clock(),
                competitors=competitors,
                budget=CompetitorCollectionBudget(
                    site_discovery_limit_each=COMPETITOR_SITE_DISCOVERY_LIMIT,
                    site_deep_limit_each=min(
                        request.generation_limits.max_competitor_pages_each,
                        COMPETITOR_SITE_DEEP_LIMIT,
                    ),
                    review_sample_limit_each=min(
                        request.generation_limits.max_review_samples_each,
                        COMPETITOR_REVIEW_SAMPLE_LIMIT,
                    ),
                    provider_attempt_limit=COMPETITOR_PROVIDER_ATTEMPT_LIMIT,
                    provider_attempts_used=attempts,
                    place_detail_calls=detail_calls,
                    review_page_calls=review_calls,
                    checkpoint_hits=checkpoint_hits,
                    truncated=truncated,
                ),
                limitations=list(dict.fromkeys(limitations)),
            )
            return result.model_dump(mode="json")

        raw = await checkpoints.run_once(job_id, final_key, operation)
        try:
            return CompetitorCollectionSnapshot.model_validate_json(canonical_json_bytes(raw))
        except (TypeError, ValueError, ValidationError) as exc:
            raise CompetitorCollectionCheckpointError() from exc


def competitor_collection_cost_counters(snapshot: CompetitorCollectionSnapshot) -> dict[str, int]:
    return {
        "competitor_discovered_pages": sum(
            item.site_inventory.discovered_url_count
            for item in snapshot.competitors
            if item.site_inventory is not None
        ),
        "competitor_deep_pages": sum(item.analyzed_page_count for item in snapshot.competitors),
        "competitor_place_detail_calls": snapshot.budget.place_detail_calls,
        "competitor_review_page_calls": snapshot.budget.review_page_calls,
        "competitor_provider_attempts": snapshot.budget.provider_attempts_used,
        "competitor_checkpoint_hits": snapshot.budget.checkpoint_hits,
    }
