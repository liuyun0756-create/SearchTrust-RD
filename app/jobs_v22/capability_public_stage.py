"""Checkpointed 2.2.2 public report generation, awaiting settlement integration."""
from uuid import UUID

from app.jobs_v22.checkpoints import JobCheckpoints
from app.jobs_v22.digest import canonical_json_bytes, request_digest
from app.jobs_v22.errors import DeterministicJobError
from app.report_v22.capability import AnalysisInputManifest, CapabilityReport
from app.report_v22.capability_public import CapabilityPublicError, build_public_capability_report
from app.report_v22.findings_models import PublicFindingsInput


class CheckpointedCapabilityPublicStage:
    """No provider call or debit; retries share the original frozen job input."""

    async def build(self, *, job_id: UUID, manifest: AnalysisInputManifest | dict,
                    request: PublicFindingsInput | dict, checkpoints: JobCheckpoints) -> CapabilityReport:
        try:
            manifest = AnalysisInputManifest.model_validate_json(canonical_json_bytes(manifest))
            if job_id != manifest.job_id:
                raise CapabilityPublicError()
            request = PublicFindingsInput.model_validate_json(canonical_json_bytes(request))
        except (ValueError, TypeError):
            raise DeterministicJobError("V22_CAPABILITY_PUBLIC_INPUT_INVALID", "The saved public sources could not be bound to this analysis.") from None
        basis = request_digest({"manifest": manifest.model_dump(mode="json"), "request": request.model_dump(mode="json")})
        key = f"v22_capability_public_stage_v1:result:{basis[7:]}"

        async def generate():
            try:
                report = build_public_capability_report(manifest, request)
            except CapabilityPublicError:
                raise DeterministicJobError("V22_CAPABILITY_PUBLIC_RESULT_INVALID", "The public evidence could not produce a valid diagnosis.") from None
            return {"schema_version": "capability_public_checkpoint_v1", "job_id": str(job_id), "input_digest": basis,
                    "result_checksum": request_digest(report), "result": report.model_dump(mode="json")}

        try:
            raw = await checkpoints.run_once(job_id, key, generate)
        except (ValueError, TypeError):
            raise DeterministicJobError("V22_CAPABILITY_PUBLIC_CHECKPOINT_INVALID", "The saved diagnosis checkpoint could not be validated.") from None
        try:
            if (not isinstance(raw, dict) or set(raw) != {"schema_version", "job_id", "input_digest", "result_checksum", "result"}
                    or len(canonical_json_bytes(raw)) > 2_100_000
                    or raw["schema_version"] != "capability_public_checkpoint_v1" or raw["job_id"] != str(job_id)
                    or raw["input_digest"] != basis or raw["result_checksum"] != request_digest(raw["result"])):
                raise ValueError("Invalid checkpoint")
            report = CapabilityReport.model_validate_json(canonical_json_bytes(raw["result"]))
            if report.input_manifest != manifest:
                raise ValueError("Different frozen input")
            return report
        except (ValueError, TypeError, KeyError):
            raise DeterministicJobError("V22_CAPABILITY_PUBLIC_CHECKPOINT_INVALID", "The saved diagnosis checkpoint could not be validated.") from None
