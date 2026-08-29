"""Validation gate linking a discovery result to one formal analysis request."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID

from pydantic import Field

from app.api.v2.competitor_models import CompetitorDiscoveryRequest
from app.api.v2.models import AnalyzeRequest
from app.competitors_v22.discovery_service import competitor_discovery_input_digest
from app.competitors_v22.market_store import SharedMarketSnapshotStore
from app.competitors_v22.normalization import canonical_competitor_url
from app.competitors_v22.store import CompetitorDiscoveryStore
from app.jobs_v22.errors import DeterministicJobError
from app.report_v22.models import StrictModel


class AnalysisDiscoveryLink(StrictModel):
    discovery_id: UUID
    candidate_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    market_snapshot_id: UUID
    market_snapshot_checksum: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")


class AnalysisRequestEnvelope(StrictModel):
    schema_version: Literal["v22_analysis_request_envelope_v1"]
    analyze_request: AnalyzeRequest
    competitor_discovery: AnalysisDiscoveryLink


class DiscoverySelectionError(DeterministicJobError):
    pass


class DiscoveryVerifier(Protocol):
    async def verify(
        self,
        *,
        discovery_id: UUID | None,
        request: AnalyzeRequest,
        now: datetime,
    ) -> AnalysisDiscoveryLink: ...


def _request_discovery_digest(request: AnalyzeRequest) -> str:
    discovery_request = CompetitorDiscoveryRequest(
        case_id=request.case_id,
        business_identity=request.business_identity,
        primary_service=request.primary_service,
        target_market=request.target_market,
        queries=request.queries,
        search_language="en",
        search_device="mobile",
        supplemental_website_urls=[],
    )
    return competitor_discovery_input_digest(discovery_request)


def _same_url(left: object, right: object) -> bool:
    return str(left).rstrip("/") == str(right).rstrip("/")


def validate_confirmed_competitors(
    *,
    request: AnalyzeRequest,
    candidates,
) -> None:
    by_id = {candidate.competitor_id: candidate for candidate in candidates}
    if len(by_id) < 3:
        raise DiscoverySelectionError(
            "COMPETITOR_DISCOVERY_NOT_READY",
            "The competitor discovery does not contain enough eligible candidates.",
        )
    seen_domains: set[str] = set()
    for confirmed in request.competitors:
        if confirmed.confirmation_source != "user":
            raise DiscoverySelectionError(
                "COMPETITOR_CONFIRMATION_REQUIRED",
                "All competitors must be explicitly confirmed by the user.",
            )
        candidate = by_id.get(confirmed.competitor_id)
        if candidate is None:
            raise DiscoverySelectionError(
                "COMPETITOR_SELECTION_INVALID",
                "A confirmed competitor is not present in the current discovery result.",
            )
        domain_url = canonical_competitor_url(str(confirmed.website_url))
        candidate_url = canonical_competitor_url(str(candidate.website_url))
        if (
            confirmed.business_name != candidate.business_name
            or domain_url != candidate_url
            or not (
                confirmed.public_gbp_url is None
                and candidate.public_gbp_url is None
                or confirmed.public_gbp_url is not None
                and candidate.public_gbp_url is not None
                and _same_url(confirmed.public_gbp_url, candidate.public_gbp_url)
            )
        ):
            raise DiscoverySelectionError(
                "COMPETITOR_SELECTION_TAMPERED",
                "A confirmed competitor does not match its canonical discovery identity.",
            )
        assert domain_url is not None
        if domain_url in seen_domains:
            raise DiscoverySelectionError(
                "COMPETITOR_SELECTION_INVALID",
                "Confirmed competitors must use distinct canonical websites.",
            )
        seen_domains.add(domain_url)


class RedisDiscoveryVerifier:
    def __init__(
        self,
        *,
        store: CompetitorDiscoveryStore,
        market_store: SharedMarketSnapshotStore,
    ) -> None:
        self.store = store
        self.market_store = market_store

    async def verify(
        self,
        *,
        discovery_id: UUID | None,
        request: AnalyzeRequest,
        now: datetime,
    ) -> AnalysisDiscoveryLink:
        if discovery_id is None:
            raise DiscoverySelectionError(
                "COMPETITOR_DISCOVERY_REQUIRED",
                "A completed competitor discovery is required before analysis.",
            )
        state = await self.store.get_state(discovery_id)
        if state is None:
            raise DiscoverySelectionError(
                "COMPETITOR_DISCOVERY_NOT_FOUND",
                "The referenced competitor discovery was not found.",
            )
        if state.status != "succeeded" or state.result is None:
            raise DiscoverySelectionError(
                "COMPETITOR_DISCOVERY_INCOMPLETE",
                "The referenced competitor discovery has not completed successfully.",
            )
        result = state.result
        if result.expires_at <= now:
            raise DiscoverySelectionError(
                "COMPETITOR_DISCOVERY_EXPIRED",
                "The competitor discovery has expired. Run discovery again.",
            )
        if result.case_id != request.case_id:
            raise DiscoverySelectionError(
                "COMPETITOR_DISCOVERY_CASE_MISMATCH",
                "The competitor discovery belongs to a different case.",
            )
        expected_digest = _request_discovery_digest(request)
        if result.input_digest != expected_digest:
            raise DiscoverySelectionError(
                "COMPETITOR_DISCOVERY_CONTEXT_MISMATCH",
                "The competitor discovery does not match the confirmed analysis context.",
            )
        if not result.ready_for_confirmation:
            raise DiscoverySelectionError(
                "COMPETITOR_DISCOVERY_NOT_READY",
                "The competitor discovery does not contain three eligible competitors.",
            )
        shared = await self.market_store.get(input_digest=result.input_digest, now=now)
        if (
            shared is None
            or shared.snapshot_id != result.market_snapshot_id
            or shared.snapshot_checksum != result.market_snapshot_checksum
        ):
            raise DiscoverySelectionError(
                "COMPETITOR_MARKET_SNAPSHOT_INVALID",
                "The competitor market snapshot is unavailable or no longer valid.",
            )
        validate_confirmed_competitors(request=request, candidates=result.candidates)
        return AnalysisDiscoveryLink(
            discovery_id=discovery_id,
            candidate_digest=result.candidate_digest,
            market_snapshot_id=result.market_snapshot_id,
            market_snapshot_checksum=result.market_snapshot_checksum,
        )
