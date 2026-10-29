"""Execute the frozen 2.2.2 branch; never create/debit another workflow."""
from app.jobs_v22.capability_input_repository import require_frozen_capability_input
from app.jobs_v22.capability_public_stage import CheckpointedCapabilityPublicStage
from app.jobs_v22.capability_service_stage import CheckpointedCapabilityServiceStage
from app.jobs_v22.capability_runtime_models import CapabilityRequestEnvelope
from app.jobs_v22.digest import canonical_json_bytes
from app.jobs_v22.errors import DeterministicJobError
from app.report_v22.capability import CapabilityReport


class CapabilityV22Executor:
    def __init__(self, *, resolver, persister):
        self.resolver=resolver
        self.persister=persister
        self.public_stage=CheckpointedCapabilityPublicStage()
        self.service_stage=CheckpointedCapabilityServiceStage()

    async def execute(self, *, job_id, request, submitted_at, checkpoints, cost_ledger=None):
        del submitted_at,cost_ledger
        try:
            envelope=CapabilityRequestEnvelope.model_validate_json(canonical_json_bytes(request))
        except (ValueError,TypeError,AttributeError):
            raise DeterministicJobError("V22_ANALYSIS_REQUEST_INVALID", "The saved analysis request could not be validated.") from None
        frozen=await self.resolver.resolve(job_id=job_id,request=envelope.capability_request,run_generation=checkpoints.run_generation)
        try:
            require_frozen_capability_input(frozen,run_generation=checkpoints.run_generation)
            m=frozen.manifest; task=envelope.capability_request
            if m.job_id!=job_id or m.case_id!=task.case_id or m.id!=task.manifest_id or m.input_checksum!=task.input_checksum:
                raise ValueError("Invalid resolver binding")
        except (ValueError,TypeError,AttributeError):
            raise DeterministicJobError("V22_CAPABILITY_INPUT_INVALID", "The frozen analysis inputs could not be validated.") from None
        if frozen.completed_report is not None:
            return frozen.completed_report
        await checkpoints.report_progress("evaluating")
        if m.mode=="acquisition":
            report=await self.public_stage.build(job_id=job_id,manifest=m,request=frozen.request,checkpoints=checkpoints)
        else:
            report=await self.service_stage.build(job_id=job_id,manifest=m,request=frozen.request,
                previous_report=frozen.previous_report,checkpoints=checkpoints)
        # Never treat a mocked/copied result as completed without full recheck.
        await checkpoints.report_progress("validating")
        checked=CapabilityReport.model_validate_json(canonical_json_bytes(report))
        if checked.input_manifest!=m: raise DeterministicJobError("V22_CAPABILITY_RESULT_INVALID", "The diagnosis did not match its saved analysis.")
        await checkpoints.report_progress("persisting")
        await self.persister.persist(user_id=m.user_id,case_id=m.case_id,job_id=job_id,manifest=m,
            run_generation=checkpoints.run_generation,report=checked)
        return checked
