"""SQL-first terminal decisions for ambiguous acknowledgement/deadline races."""
import logging

from app.jobs_v22.capability_runtime_models import CapabilityRequestEnvelope
from app.jobs_v22.digest import canonical_json_bytes
from app.report_v22.capability import CapabilityReport

logger=logging.getLogger(__name__)


class CapabilityTerminalCoordinator:
    def __init__(self, repository): self.rpc=repository.rpc

    async def decide(self, *, state, envelope, error, finish):
        task=envelope.capability_request
        raw=await self.rpc.post("finalize_v22_capability_failure",dict(p_job_id=str(state.job_id),p_case_id=str(task.case_id),
            p_manifest_id=str(task.manifest_id),p_input_checksum=task.input_checksum,p_run_generation=state.run_generation,
            p_error_code=error.error_code,p_user_message=error.user_message,p_attempt_count=min(max(state.attempt_count,1),10),p_fail=finish))
        if not isinstance(raw,dict) or set(raw)!={"status","completed_report"} or raw["status"] not in {"succeeded","failed","active"}:
            raise self.rpc.invalid() from None
        if raw["status"]=="succeeded":
            try:
                report=CapabilityReport.model_validate_json(canonical_json_bytes(raw["completed_report"]))
                m=report.input_manifest
                if (report.id!=state.job_id or m.case_id!=state.case_id or m.id!=task.manifest_id
                        or m.input_checksum!=task.input_checksum or report.report_type!=task.report_type):raise ValueError("Invalid completion")
                return report
            except (ValueError,TypeError,AttributeError):raise self.rpc.invalid() from None
        if raw["completed_report"] is not None or (finish and raw["status"]=="active"): raise self.rpc.invalid() from None
        return raw["status"]


async def capability_terminal_transition(*,store,state,error,now,coordinator,finish=True,cost_counters=None):
    """Return (handled, update). Unknown SQL outcome stays nonterminal for cron.

    In probe mode, an authoritative active result allows the ordinary takeover.
    Never let a missing/disabled coordinator fail a previously queued new job.
    """
    request=await store.get_request(state.job_id)
    if not request or request.get("schema_version")!="v22_capability_request_envelope_v1": return False,None
    try:
        if coordinator is None: return True,None
        envelope=CapabilityRequestEnvelope.model_validate_json(canonical_json_bytes(request))
        result=await coordinator.decide(state=state,envelope=envelope,error=error,finish=finish)
    except Exception as exc:
        logger.warning("capability terminal decision deferred job_id_suffix=%s error=%s",str(state.job_id)[-8:],type(exc).__name__)
        return True,None
    if result=="active": return False,None
    if isinstance(result,CapabilityReport):
        if state.status=="queued":
            running=await store.transition(state.job_id,status="running",stage="collecting_site",progress=state.progress,
                message="Restoring the completed diagnosis.",now=now,expected_generation=state.run_generation)
            if not running.applied:return True,running
        updated=await store.transition(state.job_id,status="succeeded",stage="completed",progress=100,message="Analysis complete.",
            report=result,now=now,cost_counters=cost_counters,expected_generation=state.run_generation)
    else:
        updated=await store.transition(state.job_id,status="failed",stage="failed",progress=state.progress,message=error.user_message,
            error=error,now=now,cost_counters=cost_counters,expected_generation=state.run_generation)
    return True,updated
