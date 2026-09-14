"""Deterministic composition of the frozen V22-070 through V22-074 stages."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from pydantic import ValidationError

from app.jobs_v22.checkpoints import JobCheckpoints
from app.jobs_v22.cross_source_findings_stage import CheckpointedCrossSourceFindingsStage
from app.jobs_v22.digest import canonical_json_bytes, request_digest
from app.jobs_v22.errors import DeterministicJobError
from app.jobs_v22.execution_plan_stage import CheckpointedExecutionPlanStage
from app.jobs_v22.first_party_findings_stage import CheckpointedFirstPartyFindingsStage
from app.jobs_v22.prospect_report_pipeline import build_persisted_public_findings_input
from app.jobs_v22.public_findings_stage import CheckpointedPublicFindingsStage
from app.jobs_v22.verified_input_resolver import TrustedVerifiedInput, require_trusted_verified_input
from app.jobs_v22.verified_models import VerifiedResolvedInput, VerifiedTaskRequest
from app.jobs_v22.verified_reprioritization_stage import CheckpointedVerifiedReprioritizationStage
from app.jobs_v22.version_diff_stage import CheckpointedVersionDiffStage
from app.report_v22.action_models import PublicActionPlanInput
from app.report_v22.actions import build_public_action_plan, canonical_public_findings
from app.report_v22.findings import build_public_findings
from app.report_v22.cross_source_findings_models import CrossSourceFindingsInput
from app.report_v22.execution_plan_models import ExecutionPlanBuildInput
from app.report_v22.first_party_findings_models import FirstPartyFindingsInput
from app.report_v22.first_party_checksum import semantic_first_party_input_checksum
from app.report_v22.models import ReportV22
from app.report_v22.verified_reprioritization_models import VerifiedReprioritizationInput
from app.report_v22.version_diff_models import VersionDiffBuildInput


class VerifiedReportPipeline:
    def __init__(self, *, clock=None) -> None:
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.public_stage = CheckpointedPublicFindingsStage()
        self.first_party_stage = CheckpointedFirstPartyFindingsStage()
        self.cross_source_stage = CheckpointedCrossSourceFindingsStage()
        self.reprioritization_stage = CheckpointedVerifiedReprioritizationStage()
        self.version_diff_stage = CheckpointedVersionDiffStage()
        self.execution_plan_stage = CheckpointedExecutionPlanStage()

    @staticmethod
    def _validated(*, job_id: UUID, request: VerifiedTaskRequest,
                   resolved_input: TrustedVerifiedInput,
                   run_generation: int) -> VerifiedResolvedInput:
        try:
            raw = require_trusted_verified_input(
                resolved_input, run_generation=run_generation
            )
            checked = VerifiedResolvedInput.model_validate_json(canonical_json_bytes(raw))
            checked.validate_request(job_id=job_id, request=request)
            return checked
        except (ValueError, TypeError, ValidationError, RecursionError, OverflowError):
            raise DeterministicJobError(
                "V22_VERIFIED_INPUT_INVALID",
                "The frozen Verified report inputs could not be validated.",
            ) from None

    @staticmethod
    def _validate_parent_public(parent: ReportV22, public_input) -> None:
        try:
            findings = build_public_findings(public_input)
            plan = build_public_action_plan(PublicActionPlanInput(
                findings_result=findings, planning_date=parent.report_version.generated_at.date()))
            if (canonical_json_bytes([item.model_dump(mode="json") for item in parent.findings])
                    != canonical_json_bytes([item.model_dump(mode="json") for item in findings.findings])
                    or canonical_json_bytes([item.model_dump(mode="json") for item in parent.evidence_index])
                    != canonical_json_bytes([item.model_dump(mode="json")
                        for item in findings.evidence_result.evidence_index])):
                raise ValueError
            expected = {item.action_id: item for item in plan.actions}
            if set(expected) != {item.action_id for item in parent.top_actions}:
                raise ValueError
            for actual in parent.top_actions:
                source = expected[actual.action_id]
                targets = [str(item.url) if item.kind in {"url", "site"} else
                    (item.query or "") if item.kind == "query" else
                    (item.page_type or "") if item.kind == "page_type" else
                    f"Google Business Profile field: {item.gbp_field}" for item in source.exact_targets]
                metrics = [{"metric_key": item.metric_key, "baseline": item.baseline,
                    "success_condition": item.success_condition, "source_type": item.source_types[0]}
                    for item in source.validation_metrics]
                if (actual.sequence != source.sequence or actual.finding_ids != source.finding_ids
                        or actual.exact_targets != targets
                        or actual.implementation_steps != source.implementation_steps
                        or actual.specification != source.specification
                        or actual.required_client_assets != source.required_client_assets
                        or actual.dependencies != source.dependencies
                        or actual.owner_suggestion != source.owner_suggestion
                        or actual.effort_bucket != source.effort_bucket
                        or actual.definition_of_done != source.definition_of_done
                        or [item.model_dump(mode="json") for item in actual.validation_metrics] != metrics
                        or actual.data_sources != source.data_sources
                        or actual.review_date != source.review_date):
                    raise ValueError
        except Exception:
            raise DeterministicJobError("V22_VERIFIED_PARENT_PUBLIC_MISMATCH",
                "The frozen public sources no longer reproduce the parent report.") from None

    async def build(self, *, job_id: UUID, request: VerifiedTaskRequest,
                    resolved_input: TrustedVerifiedInput, checkpoints: JobCheckpoints) -> ReportV22:
        resolved = self._validated(
            job_id=job_id,
            request=request,
            resolved_input=resolved_input,
            run_generation=(
                checkpoints.run_generation
                if checkpoints.run_generation is not None
                else 1
            ),
        )
        parent = resolved.parent_report

        public_input = build_persisted_public_findings_input(
            request=resolved.analyze_request,
            site_inventory=resolved.site_snapshot.normalized_payload,
            site_snapshot_id=resolved.site_snapshot.snapshot_id,
            shared_market=resolved.shared_market,
            competitor_collection=resolved.competitor_snapshot.normalized_payload,
            competitor_snapshot_id=resolved.competitor_snapshot.snapshot_id,
            public_gbp_snapshot=resolved.public_gbp_snapshot.normalized_payload,
            public_gbp_snapshot_id=resolved.public_gbp_snapshot.snapshot_id,
            public_gbp_reference=resolved.public_gbp_snapshot.reference,
            evaluated_at=parent.report_version.generated_at,
        )
        self._validate_parent_public(parent, public_input)

        async def capture_evaluated_at():
            value = self.clock()
            if value.tzinfo is None or value.utcoffset() is None:
                raise DeterministicJobError("V22_VERIFIED_CLOCK_INVALID", "Verified evaluation time is invalid.")
            return {"schema_version": "verified_evaluation_time_v1", "job_id": str(job_id),
                "evaluated_at": value.isoformat()}

        saved_time = await checkpoints.run_once(
            job_id, "v22_verified_evaluation_time_v1", capture_evaluated_at)
        try:
            if (set(saved_time) != {"schema_version", "job_id", "evaluated_at"}
                    or saved_time["schema_version"] != "verified_evaluation_time_v1"
                    or saved_time["job_id"] != str(job_id)):
                raise ValueError
            evaluated_at = datetime.fromisoformat(saved_time["evaluated_at"])
            if evaluated_at.tzinfo is None or evaluated_at.utcoffset() is None:
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            raise DeterministicJobError(
                "V22_VERIFIED_CLOCK_CHECKPOINT_INVALID",
                "The saved Verified evaluation time could not be validated.",
            ) from None
        public_result = await self.public_stage.build(
            job_id=job_id, request=public_input, checkpoints=checkpoints)
        public_plan = build_public_action_plan(PublicActionPlanInput(
            findings_result=public_result,
            planning_date=parent.report_version.generated_at.date(),
        ))

        # Resolver order is a transport detail. Freeze the semantic source order at
        # the pipeline boundary so every nested V22-071…074 checkpoint is stable.
        first_party_snapshots = sorted(
            resolved.first_party_snapshots,
            key=lambda snapshot: (snapshot.source_type, str(snapshot.snapshot_id)),
        )
        first_input = FirstPartyFindingsInput(
            case_id=resolved.case_id,
            parent_report_id=parent.report_version.report_id,
            evaluated_at=evaluated_at,
            snapshots=first_party_snapshots,
        )
        first_result = await self.first_party_stage.build(
            job_id=job_id, request=first_input, checkpoints=checkpoints)
        first_checksum = semantic_first_party_input_checksum(first_input)
        cross_input = CrossSourceFindingsInput(
            case_id=resolved.case_id,
            parent_report_id=parent.report_version.report_id,
            evaluated_at=evaluated_at,
            normalized_domain=parent.identity.business.normalized_domain,
            first_party_input=first_input,
            first_party_result=first_result,
            first_party_input_checksum=first_checksum,
            first_party_result_checksum=request_digest(first_result),
        )
        cross_result = await self.cross_source_stage.build(
            job_id=job_id, request=cross_input, checkpoints=checkpoints)
        verified_input = VerifiedReprioritizationInput(
            case_id=resolved.case_id,
            parent_report_id=parent.report_version.report_id,
            evaluated_at=evaluated_at,
            planning_date=evaluated_at.date(),
            public_findings_input=public_input,
            public_findings_result=public_result,
            public_findings_input_checksum=request_digest(public_input),
            public_findings_result_checksum=request_digest(canonical_public_findings(public_result)),
            public_action_plan=public_plan,
            public_action_plan_checksum=request_digest(public_plan),
            first_party_input=first_input,
            first_party_result=first_result,
            first_party_input_checksum=first_checksum,
            first_party_result_checksum=request_digest(first_result),
            cross_source_normalized_domain=parent.identity.business.normalized_domain,
            cross_source_result=cross_result,
            cross_source_result_checksum=request_digest(cross_result),
        )
        verified_result = await self.reprioritization_stage.build(
            job_id=job_id, request=verified_input, checkpoints=checkpoints)
        diff_input = VersionDiffBuildInput(
            case_id=resolved.case_id,
            parent_report_id=parent.report_version.report_id,
            evaluated_at=evaluated_at,
            parent_report=parent,
            parent_report_checksum=request_digest(parent),
            verified_reprioritization_input=verified_input,
            verified_reprioritization_input_checksum=request_digest(verified_input),
            verified_reprioritization_result=verified_result,
            verified_reprioritization_result_checksum=request_digest(verified_result),
        )
        diff_result = await self.version_diff_stage.build(
            job_id=job_id, request=diff_input, checkpoints=checkpoints)
        execution_input = ExecutionPlanBuildInput(
            case_id=resolved.case_id,
            parent_report_id=parent.report_version.report_id,
            verified_report_id=job_id,
            evaluated_at=evaluated_at,
            planning_date=evaluated_at.date(),
            verified_reprioritization_input=verified_input,
            verified_reprioritization_input_checksum=request_digest(verified_input),
            verified_reprioritization_result=verified_result,
            verified_reprioritization_result_checksum=request_digest(verified_result),
            version_diff_input=diff_input,
            version_diff_input_checksum=request_digest(diff_input),
            version_diff_result=diff_result,
            version_diff_result_checksum=request_digest(diff_result),
            copy_model_version="v22_verified_copy_v1",
        )
        execution_result = await self.execution_plan_stage.build(
            job_id=job_id, request=execution_input, checkpoints=checkpoints)
        return execution_result.report
