"""Execution boundaries for the isolated SearchTrust v2.2 pipeline."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Protocol
from uuid import UUID

from pydantic import ValidationError

from app.api.v2.models import AnalyzeRequest
from app.api.v2.competitor_models import CompetitorDiscoveryResult
from app.collectors.site_inventory_models import SiteInventorySnapshot
from app.competitors_v22.models import CompetitorCollectionSnapshot, SharedMarketSnapshot
from app.competitors_v22.selection import AnalysisRequestEnvelope
from app.jobs_v22.checkpoints import JobCheckpoints
from app.jobs_v22.digest import canonical_json_bytes
from app.jobs_v22.errors import DeterministicJobError
from app.report_v22.models import ReportV22


ExecutorRequest = dict[str, Any] | AnalysisRequestEnvelope


class V22JobExecutor(Protocol):
    async def execute(
        self,
        *,
        job_id: UUID,
        request: ExecutorRequest,
        submitted_at: datetime,
        checkpoints: JobCheckpoints,
    ) -> ReportV22: ...


class DiscoveryStore(Protocol):
    async def require_state(self, discovery_id: UUID) -> Any: ...


class MarketStore(Protocol):
    async def get(
        self,
        *,
        input_digest: str,
        now: datetime,
    ) -> SharedMarketSnapshot | None: ...


class SiteStage(Protocol):
    async def collect(
        self,
        *,
        job_id: UUID,
        request: AnalyzeRequest,
        checkpoints: JobCheckpoints,
    ) -> SiteInventorySnapshot: ...


class CompetitorStage(Protocol):
    async def collect(
        self,
        *,
        job_id: UUID,
        request: AnalyzeRequest,
        discovery: CompetitorDiscoveryResult,
        shared_market: SharedMarketSnapshot,
        checkpoints: JobCheckpoints,
    ) -> CompetitorCollectionSnapshot: ...


class ProspectReportPipeline(Protocol):
    async def build(
        self,
        *,
        job_id: UUID,
        request: AnalyzeRequest,
        site_inventory: SiteInventorySnapshot,
        discovery: CompetitorDiscoveryResult,
        shared_market: SharedMarketSnapshot,
        competitor_collection: CompetitorCollectionSnapshot,
        submitted_at: datetime,
        checkpoints: JobCheckpoints,
    ) -> ReportV22: ...


class ProspectV22Executor:
    """Run a prospect report from the exact discovery snapshot the user confirmed."""

    def __init__(
        self,
        *,
        discovery_store: DiscoveryStore,
        market_store: MarketStore,
        site_stage: SiteStage,
        competitor_stage: CompetitorStage,
        report_pipeline: ProspectReportPipeline,
        clock=None,
    ) -> None:
        self.discovery_store = discovery_store
        self.market_store = market_store
        self.site_stage = site_stage
        self.competitor_stage = competitor_stage
        self.report_pipeline = report_pipeline
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    @staticmethod
    def _validate_envelope(request: ExecutorRequest) -> AnalysisRequestEnvelope:
        try:
            if isinstance(request, AnalysisRequestEnvelope):
                return AnalysisRequestEnvelope.model_validate(
                    request.model_dump(mode="python")
                )
            return AnalysisRequestEnvelope.model_validate_json(canonical_json_bytes(request))
        except (TypeError, ValueError, ValidationError):
            raise DeterministicJobError(
                "V22_ANALYSIS_REQUEST_INVALID",
                "The saved analysis request could not be validated.",
            ) from None

    @staticmethod
    def _validate_discovery_link(envelope: AnalysisRequestEnvelope, state: Any):
        result = getattr(state, "result", None)
        link = envelope.competitor_discovery
        if (
            getattr(state, "status", None) != "succeeded"
            or not isinstance(result, CompetitorDiscoveryResult)
            or result.discovery_id != link.discovery_id
            or result.candidate_digest != link.candidate_digest
            or result.market_snapshot_id != link.market_snapshot_id
            or result.market_snapshot_checksum != link.market_snapshot_checksum
        ):
            raise DeterministicJobError(
                "V22_COMPETITOR_DISCOVERY_INVALID",
                "The confirmed competitor discovery is unavailable or has changed.",
            )
        return result

    async def execute(
        self,
        *,
        job_id: UUID,
        request: ExecutorRequest,
        submitted_at: datetime,
        checkpoints: JobCheckpoints,
    ) -> ReportV22:
        envelope = self._validate_envelope(request)
        analyze = envelope.analyze_request
        if analyze.report_type != "prospect":
            raise DeterministicJobError(
                "V22_ANALYSIS_MODE_UNSUPPORTED",
                "This worker currently supports prospect reports only.",
            )

        state = await self.discovery_store.require_state(
            envelope.competitor_discovery.discovery_id
        )
        discovery = self._validate_discovery_link(envelope, state)
        shared_market = await self.market_store.get(
            input_digest=discovery.input_digest,
            now=self.clock(),
        )
        link = envelope.competitor_discovery
        if (
            shared_market is None
            or shared_market.snapshot_id != link.market_snapshot_id
            or shared_market.snapshot_checksum != link.market_snapshot_checksum
            or shared_market.input_digest != discovery.input_digest
        ):
            raise DeterministicJobError(
                "V22_COMPETITOR_MARKET_SNAPSHOT_INVALID",
                "The confirmed market snapshot is unavailable or has expired.",
            )

        site_inventory = await self.site_stage.collect(
            job_id=job_id,
            request=analyze,
            checkpoints=checkpoints,
        )
        competitor_collection = await self.competitor_stage.collect(
            job_id=job_id,
            request=analyze,
            discovery=discovery,
            shared_market=shared_market,
            checkpoints=checkpoints,
        )
        report = await self.report_pipeline.build(
            job_id=job_id,
            request=analyze,
            site_inventory=site_inventory,
            discovery=discovery,
            shared_market=shared_market,
            competitor_collection=competitor_collection,
            submitted_at=submitted_at,
            checkpoints=checkpoints,
        )
        try:
            return ReportV22.model_validate(report.model_dump(mode="python"))
        except (AttributeError, TypeError, ValueError, ValidationError):
            raise DeterministicJobError(
                "V22_REPORT_INVALID",
                "The analysis result did not satisfy the v2.2 report contract.",
            ) from None


class UnavailableV22Executor:
    """Production-safe placeholder until the real v2.2 pipeline is delivered."""

    async def execute(
        self,
        *,
        job_id: UUID,
        request: ExecutorRequest,
        submitted_at: datetime,
        checkpoints: JobCheckpoints | None,
    ) -> ReportV22:
        raise DeterministicJobError(
            "V22_PIPELINE_NOT_READY",
            "SearchTrust v2.2 analysis is not available yet.",
        )
