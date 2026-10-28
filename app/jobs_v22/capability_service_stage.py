"""Checkpointed service report generation; no intake/payment path is enabled."""
from uuid import UUID

from app.jobs_v22.checkpoints import JobCheckpoints
from app.jobs_v22.digest import canonical_json_bytes, request_digest, verified_request_digest
from app.jobs_v22.errors import DeterministicJobError
from app.report_v22.capability import AnalysisInputManifest, CapabilityReport
from app.report_v22.capability_service import CapabilityServiceError, build_service_capability_report
from app.report_v22.capability_service_inputs import CapabilityServiceInput


class CheckpointedCapabilityServiceStage:
    async def build(self, *, job_id: UUID, manifest, request, checkpoints: JobCheckpoints, previous_report=None) -> CapabilityReport:
        try:
            manifest=AnalysisInputManifest.model_validate_json(canonical_json_bytes(manifest))
            request=CapabilityServiceInput.model_validate_json(canonical_json_bytes(request))
            if job_id != manifest.job_id: raise ValueError("Wrong job")
            basis=request_digest({"manifest":manifest.model_dump(mode="json"),"request":request.model_dump(mode="json"),
                "previous_payload_checksum":verified_request_digest(previous_report) if previous_report is not None else None})
        except (ValueError,TypeError):
            raise DeterministicJobError("V22_CAPABILITY_SERVICE_INPUT_INVALID","The saved service sources could not be bound to this analysis.") from None
        key=f"v22_capability_service_stage_v1:result:{basis[7:]}"

        async def generate():
            try: report=build_service_capability_report(manifest,request,previous_report=previous_report)
            except CapabilityServiceError:
                raise DeterministicJobError("V22_CAPABILITY_SERVICE_RESULT_INVALID","The connected evidence could not produce a valid service diagnosis.") from None
            return {"schema_version":"capability_service_checkpoint_v1","job_id":str(job_id),"input_digest":basis,
                "result_checksum":request_digest(report),"result":report.model_dump(mode="json")}

        try: raw=await checkpoints.run_once(job_id,key,generate)
        except (ValueError,TypeError):
            raise DeterministicJobError("V22_CAPABILITY_SERVICE_CHECKPOINT_INVALID","The saved service diagnosis checkpoint could not be validated.") from None
        try:
            if (not isinstance(raw,dict) or set(raw)!={"schema_version","job_id","input_digest","result_checksum","result"}
                    or len(canonical_json_bytes(raw))>2_100_000 or raw["schema_version"]!="capability_service_checkpoint_v1"
                    or raw["job_id"]!=str(job_id) or raw["input_digest"]!=basis or raw["result_checksum"]!=request_digest(raw["result"])):
                raise ValueError("Invalid checkpoint")
            report=CapabilityReport.model_validate_json(canonical_json_bytes(raw["result"]))
            if report.input_manifest!=manifest: raise ValueError("Different manifest")
            return report
        except (ValueError,TypeError,KeyError):
            raise DeterministicJobError("V22_CAPABILITY_SERVICE_CHECKPOINT_INVALID","The saved service diagnosis checkpoint could not be validated.") from None
