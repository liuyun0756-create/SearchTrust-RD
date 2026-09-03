"""Bounded, first-party-free website inventory for confirmed competitors."""

from __future__ import annotations

import asyncio
from typing import Literal, Protocol
from uuid import UUID

from pydantic import Field, model_validator

from app.api.v2.models import ConfirmedCompetitor
from app.collectors.site_inventory_models import GscPagePriority, SiteInventorySnapshot
from app.competitors_v22.models import (
    COMPETITOR_MAX_COUNT,
    COMPETITOR_MIN_COUNT,
    COMPETITOR_SITE_DEEP_LIMIT,
    COMPETITOR_SITE_DISCOVERY_LIMIT,
)
from app.competitors_v22.normalization import normalize_domain
from app.jobs_v22.checkpoints import JobCheckpoints
from app.jobs_v22.errors import DeterministicJobError
from app.report_v22.models import CompetitorId, StrictModel, TargetMarket


class SiteCollector(Protocol):
    async def collect_inventory_for_site(
        self,
        *,
        job_id: UUID,
        site_url: str,
        primary_service: str,
        target_market: str,
        discovery_limit: int,
        deep_analysis_limit: int,
        gsc_priorities: list[GscPagePriority],
        checkpoints: JobCheckpoints,
        checkpoint_namespace: str,
    ) -> SiteInventorySnapshot: ...


class CompetitorSiteInventoryResult(StrictModel):
    competitor_id: CompetitorId
    status: Literal["available", "partial", "unavailable"]
    inventory: SiteInventorySnapshot | None = None
    limitations: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def validate_result(self) -> "CompetitorSiteInventoryResult":
        if self.inventory is None and self.status != "unavailable":
            raise ValueError("missing competitor inventory must be unavailable")
        if self.inventory is not None and self.status == "unavailable":
            raise ValueError("available competitor inventory cannot be unavailable")
        return self


def _market_text(target_market: TargetMarket) -> str:
    return " ".join(
        value
        for value in (
            target_market.display_name,
            target_market.city,
            target_market.region,
            target_market.country_code,
        )
        if value
    )


class CheckpointedCompetitorSiteStage:
    def __init__(self, collector: SiteCollector) -> None:
        self.collector = collector

    async def collect(
        self,
        *,
        job_id: UUID,
        competitors: list[ConfirmedCompetitor],
        primary_service: str,
        target_market: TargetMarket,
        max_pages_each: int,
        checkpoints: JobCheckpoints,
    ) -> list[CompetitorSiteInventoryResult]:
        if not COMPETITOR_MIN_COUNT <= len(competitors) <= COMPETITOR_MAX_COUNT:
            raise ValueError("competitor site collection requires one to three competitors")
        deep_limit = min(max_pages_each, COMPETITOR_SITE_DEEP_LIMIT)
        if deep_limit < 1:
            raise ValueError("competitor deep page limit must be positive")

        async def collect_one(competitor: ConfirmedCompetitor) -> CompetitorSiteInventoryResult:
            try:
                inventory = await self.collector.collect_inventory_for_site(
                    job_id=job_id,
                    site_url=str(competitor.website_url),
                    primary_service=primary_service,
                    target_market=_market_text(target_market),
                    discovery_limit=COMPETITOR_SITE_DISCOVERY_LIMIT,
                    deep_analysis_limit=deep_limit,
                    gsc_priorities=[],
                    checkpoints=checkpoints,
                    checkpoint_namespace=f"competitor:{competitor.competitor_id}",
                )
                if inventory.canonical_host != normalize_domain(str(competitor.website_url)):
                    raise ValueError("competitor inventory crossed its canonical website boundary")
            except asyncio.CancelledError:
                raise
            except (DeterministicJobError, OSError, ValueError):
                return CompetitorSiteInventoryResult(
                    competitor_id=competitor.competitor_id,
                    status="unavailable",
                    limitations=["The competitor website could not be collected within safe limits."],
                )
            status = "available" if not inventory.limitations else "partial"
            return CompetitorSiteInventoryResult(
                competitor_id=competitor.competitor_id,
                status=status,
                inventory=inventory,
                limitations=list(inventory.limitations),
            )

        return list(await asyncio.gather(*(collect_one(item) for item in competitors)))
