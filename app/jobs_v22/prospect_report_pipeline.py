"""Checkpointed public evidence, findings, actions, copy, and report assembly."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from app.api.v2.competitor_models import CompetitorDiscoveryResult
from app.api.v2.models import AnalyzeRequest
from app.collectors.site_inventory_models import SiteInventorySnapshot
from app.competitors_v22.models import CompetitorCollectionSnapshot, SharedMarketSnapshot
from app.jobs_v22.checkpoints import JobCheckpoints
from app.jobs_v22.digest import request_digest
from app.jobs_v22.public_findings_stage import CheckpointedPublicFindingsStage
from app.report_v22.action_models import PublicActionPlanInput
from app.report_v22.actions import build_public_action_plan
from app.report_v22.assembler import assemble_prospect_report
from app.report_v22.copy_contract import build_copy_request, validate_and_render_copy
from app.report_v22.copy_models import CopyRequestV1
from app.report_v22.evidence_models import (
    CompetitorEvidenceSource,
    EvidenceBuildContext,
    EvidenceBuildInput,
    MissingEvidenceSource,
    SerpEvidenceSource,
    SiteEvidenceSource,
    SnapshotBinding,
)
from app.report_v22.findings_models import PublicFindingsInput
from app.report_v22.models import ReportV22
from app.report_v22.public_gbp_models import CustomerPublicGbpReference


class ControlledCopyProvider(Protocol):
    model_version: str

    async def generate(self, *, job_id: UUID, request: CopyRequestV1) -> object: ...


def _snapshot_id(kind: str, checksum: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"searchtrust:v22:{kind}:{checksum}")


def _binding(
    *,
    snapshot_id: UUID,
    case_id: UUID,
    source_type: str,
    schema_version: str,
    checksum: str,
    fetched_at: datetime,
    expires_at: datetime | None = None,
) -> SnapshotBinding:
    return SnapshotBinding(
        snapshot_id=snapshot_id,
        case_id=case_id,
        source_type=source_type,
        schema_version=schema_version,
        payload_checksum=checksum,
        fetched_at=fetched_at,
        expires_at=expires_at,
        health_status="healthy",
        identity_match_status="matched",
    )


def build_prospect_evidence_input(
    *,
    request: AnalyzeRequest,
    site_inventory: SiteInventorySnapshot,
    shared_market: SharedMarketSnapshot,
    competitor_collection: CompetitorCollectionSnapshot,
    evaluated_at: datetime,
    confirmed_at: datetime,
) -> EvidenceBuildInput:
    """Bind collected snapshots to one case before any public rule can use them."""

    site_checksum = request_digest(site_inventory)
    competitor_checksum = request_digest(competitor_collection)
    public_reference = None
    if request.business_identity.public_gbp_url is not None:
        public_reference = CustomerPublicGbpReference(
            case_id=request.case_id,
            site_url=request.business_identity.site_url,
            public_gbp_url=request.business_identity.public_gbp_url,
            entity_keys=[],
            confirmation_source="user",
            confirmed_at=confirmed_at,
        )
    context = EvidenceBuildContext(
        case_id=request.case_id,
        report_type="prospect",
        site_url=request.business_identity.site_url,
        primary_service=request.primary_service,
        target_market=request.target_market,
        queries=request.queries,
        search_language=shared_market.snapshot.language,
        search_device=shared_market.snapshot.device,
        competitors=request.competitors,
        evaluated_at=evaluated_at,
        customer_public_gbp=public_reference,
    )
    sources = [
        SiteEvidenceSource(
            binding=_binding(
                snapshot_id=_snapshot_id("site", site_checksum),
                case_id=request.case_id,
                source_type="site",
                schema_version=site_inventory.schema_version,
                checksum=site_checksum,
                fetched_at=site_inventory.completed_at,
            ),
            payload=site_inventory,
        ),
        SerpEvidenceSource(
            binding=_binding(
                snapshot_id=shared_market.snapshot_id,
                case_id=request.case_id,
                source_type="serp",
                schema_version=shared_market.snapshot.schema_version,
                checksum=shared_market.snapshot_checksum,
                fetched_at=shared_market.snapshot.completed_at,
                expires_at=shared_market.expires_at,
            ),
            payload=shared_market.snapshot,
            shared_snapshot=shared_market,
        ),
        CompetitorEvidenceSource(
            binding=_binding(
                snapshot_id=_snapshot_id("competitor", competitor_checksum),
                case_id=request.case_id,
                source_type="competitor",
                schema_version=competitor_collection.schema_version,
                checksum=competitor_checksum,
                fetched_at=competitor_collection.completed_at,
            ),
            payload=competitor_collection,
        ),
    ]
    missing = [
        MissingEvidenceSource(
            source_type="gbp",
            gbp_origin="public_profile",
            reason="no_snapshot",
        ),
        MissingEvidenceSource(source_type="gsc", reason="not_connected"),
        MissingEvidenceSource(
            source_type="gbp",
            gbp_origin="first_party",
            reason="not_connected",
        ),
        MissingEvidenceSource(source_type="ga4", reason="not_connected"),
    ]
    return EvidenceBuildInput(context=context, sources=sources, missing_sources=missing)


class PublicProspectReportPipeline:
    def __init__(
        self,
        *,
        copy_provider: ControlledCopyProvider,
        findings_stage: CheckpointedPublicFindingsStage | None = None,
        clock=None,
    ) -> None:
        self.copy_provider = copy_provider
        self.findings_stage = findings_stage or CheckpointedPublicFindingsStage()
        self.clock = clock or (lambda: datetime.now(timezone.utc))

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
    ) -> ReportV22:
        del discovery  # Its immutable identities were checked by the executor and collection stage.
        generated_at = self.clock()
        evidence_input = build_prospect_evidence_input(
            request=request,
            site_inventory=site_inventory,
            shared_market=shared_market,
            competitor_collection=competitor_collection,
            evaluated_at=generated_at,
            confirmed_at=submitted_at,
        )
        findings = await self.findings_stage.build(
            job_id=job_id,
            request=PublicFindingsInput(
                evidence_input=evidence_input,
                business_identity=request.business_identity,
            ),
            checkpoints=checkpoints,
        )
        action_plan = build_public_action_plan(
            PublicActionPlanInput(
                findings_result=findings,
                planning_date=generated_at.date(),
            )
        )
        copy_request = build_copy_request(findings, action_plan)
        copy_key = f"v22_controlled_copy_v1:result:{request_digest(copy_request)[7:]}"

        async def generate_copy() -> Any:
            return await self.copy_provider.generate(job_id=job_id, request=copy_request)

        copy_outputs = await checkpoints.run_once(job_id, copy_key, generate_copy)
        action_copy = validate_and_render_copy(copy_request, copy_outputs)
        return assemble_prospect_report(
            report_id=job_id,
            request=request,
            site_inventory=site_inventory,
            shared_market=shared_market,
            competitor_collection=competitor_collection,
            findings=findings,
            action_plan=action_plan,
            action_copy=action_copy,
            generated_at=generated_at,
            copy_model_version=self.copy_provider.model_version,
        )
