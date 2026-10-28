"""Confirmed acquisition intake: recover first, verify once, atomically enroll."""
from uuid import UUID

from pydantic import Field

from app.api.v2.models import TaskStatusResponse
from app.competitors_v22.selection import AnalysisRequestEnvelope
from app.jobs_v22.capability_runtime_models import CapabilityTaskStatusResponse
from app.jobs_v22.digest import canonical_json_bytes, verified_canonical_json_bytes
from app.jobs_v22.errors import DeterministicJobError
from app.report_v22.models import StrictModel


class IntakeReceipt(StrictModel):
    business_version_id: UUID
    request: AnalysisRequestEnvelope
    run_generation: int = Field(ge=1)
    status: str


async def submit_acquisition(
    *, repository, verifier, queue, user_id, job_id, workflow_id,
    discovery_id, idempotency_key, previous_job_id, request, now, planning_revision=None,
):
    if request.report_type != "prospect" or (planning_revision is not None and (type(planning_revision) is not int or not 0 <= planning_revision <= 2147483647)):
        raise DeterministicJobError("CAPABILITY_INTAKE_INVALID", "Only confirmed acquisition work can start here.")
    identity = dict(
        p_user_id=str(user_id), p_case_id=str(request.case_id),
        p_workflow_id=str(workflow_id), p_job_id=str(job_id),
        p_idempotency_key=idempotency_key, p_discovery_id=str(discovery_id),
        p_previous_job_id=str(previous_job_id) if previous_job_id else None,
    )
    raw = await repository.rpc.post("lookup_v22_acquisition_intake", {
        **identity, "p_analyze_request": request.model_dump(mode="json")})
    if raw is None:
        link = await verifier.verify(discovery_id=discovery_id, request=request, now=now)
        if link.discovery_id != discovery_id:
            raise repository.rpc.invalid()
        envelope = AnalysisRequestEnvelope(
            schema_version="v22_analysis_request_envelope_v1",
            analyze_request=request, competitor_discovery=link,
        )
        canonical = verified_canonical_json_bytes(envelope.model_dump(mode="json"))
        if len(canonical) > 150_000:
            raise DeterministicJobError("CAPABILITY_INTAKE_INVALID", "The analysis request is too large.")
        raw = await repository.rpc.post("start_v22_acquisition_intake_with_planning" if planning_revision is not None else "start_v22_acquisition_intake", {
            **identity, "p_request_canonical": canonical.decode(),
            **({"p_planning_revision": planning_revision} if planning_revision is not None else {})})
    try:
        receipt = IntakeReceipt.model_validate_json(canonical_json_bytes(raw))
        if (receipt.request.analyze_request != request
                or receipt.request.competitor_discovery.discovery_id != discovery_id
                or receipt.status not in {"queued", "running", "succeeded", "failed"}):
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise repository.rpc.invalid() from None
    if receipt.status in {"queued", "running"}:
        # Requeue the SAME preparation ID on replay. SQL ready recovery handles
        # prepared sources; never bind the large envelope to the formal store.
        await queue.enqueue(job_id, receipt.run_generation)
    raw_status = await repository.rpc.post("get_v22_acquisition_status", {
        "p_user_id": str(user_id), "p_job_id": str(job_id)})
    try:
        model = (CapabilityTaskStatusResponse
                 if isinstance(raw_status, dict) and "schema_version" in raw_status
                 else TaskStatusResponse)
        value = model.model_validate_json(canonical_json_bytes(raw_status))
        if value.job_id != job_id or value.run_generation != receipt.run_generation:
            raise ValueError()
        if isinstance(value,CapabilityTaskStatusResponse):
            if (value.report_reference.case_id != request.case_id
                    or value.report_reference.report_type != "prospect"):
                raise ValueError()
        elif value.report is not None:
            raise ValueError()
        return value
    except (ValueError, TypeError, AttributeError):
        raise repository.rpc.invalid() from None
