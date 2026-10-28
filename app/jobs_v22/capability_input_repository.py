"""Bounded service-role prepare/resolve boundary for immutable worker inputs."""
from dataclasses import dataclass
from uuid import UUID
from weakref import WeakKeyDictionary

import httpx

from app.jobs_v22.capability_inputs import generation_recipe, validate_generation_input
from app.jobs_v22.capability_runtime_models import CapabilityTaskRequest
from app.jobs_v22.digest import canonical_json_bytes, request_digest, verified_canonical_json_bytes
from app.jobs_v22.errors import DeterministicJobError
from app.jobs_v22.verified_input_resolver import VerifiedRpcClient
from app.report_v22.capability import AnalysisInputManifest, CapabilityReport


@dataclass(frozen=True, eq=False, slots=True, weakref_slot=True)
class FrozenCapabilityInput:
    manifest: AnalysisInputManifest
    request: object | None
    previous_report: dict | None
    completed_report: CapabilityReport | None


_SEALS: WeakKeyDictionary[FrozenCapabilityInput, tuple[int, str]] = WeakKeyDictionary()


def _digest(value):
    return request_digest(dict(manifest=value.manifest.model_dump(mode="json"),
        request=value.request.model_dump(mode="json") if value.request is not None else None,
        previous_report=value.previous_report,
        completed_report=value.completed_report.model_dump(mode="json") if value.completed_report is not None else None))


def require_frozen_capability_input(value, *, run_generation):
    if type(value) is not FrozenCapabilityInput or _SEALS.get(value) != (run_generation, _digest(value)):
        raise ValueError("Input was not returned intact by the authorized resolver")
    return value


class SupabaseCapabilityInputRepository:
    def __init__(self, *, url: str, service_role_key: str, http_client: httpx.AsyncClient):
        self.rpc = VerifiedRpcClient(url=url, service_role_key=service_role_key, http_client=http_client,
            max_response_bytes=25_000_000, prefix="V22_CAPABILITY_INPUT", invalid_retryable=True, total_timeout_seconds=60)

    async def prepare(self, *, manifest, request, run_generation: int, previous_report=None):
        try:
            bound, value, recipe = generation_recipe(manifest, request, previous_report)
            canonical = verified_canonical_json_bytes(value.model_dump(mode="json"))
            if type(run_generation) is not int or run_generation < 1 or len(canonical)>20_000_000:
                raise ValueError("Invalid generation")
            public = value if bound.mode=="acquisition" else value.public_input
            sources = {s.kind: canonical_json_bytes(s.payload).decode() for s in public.evidence_input.sources}
        except (ValueError, TypeError, AttributeError, OverflowError, RecursionError):
            raise DeterministicJobError("V22_CAPABILITY_INPUT_INVALID", "The saved analysis sources could not be prepared.") from None
        rows = await self.rpc.post("prepare_v22_capability_analysis_input", dict(
            p_user_id=str(bound.user_id), p_case_id=str(bound.case_id), p_job_id=str(bound.job_id),
            p_manifest=bound.model_dump(mode="json"), p_manifest_canonical=verified_canonical_json_bytes(bound.model_dump(mode="json",exclude={"input_checksum"})).decode(),
            p_recipe=recipe, p_generation_canonical=canonical.decode(), p_public_source_canonicals=sources,
            p_previous_canonical=verified_canonical_json_bytes(previous_report).decode() if previous_report is not None else None,
            p_run_generation=run_generation))
        row = rows[0] if isinstance(rows,list) and len(rows)==1 else rows
        if not isinstance(row,dict) or row != dict(manifest_id=str(bound.id), input_checksum=bound.input_checksum,
                generation_input_checksum=bound.generation_input_checksum):
            raise self.rpc.invalid() from None
        return bound

    async def resolve(self, *, job_id: UUID, request: CapabilityTaskRequest, run_generation: int):
        try:
            task = CapabilityTaskRequest.model_validate_json(canonical_json_bytes(request))
            if type(run_generation) is not int or run_generation<1: raise ValueError("Invalid generation")
        except (ValueError,TypeError,AttributeError):
            raise DeterministicJobError("V22_CAPABILITY_INPUT_INVALID", "The saved analysis request could not be validated.") from None
        raw = await self.rpc.post("resolve_v22_capability_analysis_input", dict(p_job_id=str(job_id),p_case_id=str(task.case_id),
            p_manifest_id=str(task.manifest_id),p_input_checksum=task.input_checksum,p_run_generation=run_generation))
        try:
            if (not isinstance(raw,dict) or set(raw)!={"schema_version","manifest","run_generation","request","previous_report","completed_report"}
                    or raw["schema_version"]!="v22_capability_resolved_input_v1" or type(raw["run_generation"]) is not int
                    or raw["run_generation"]!=run_generation): raise ValueError("Invalid resolver envelope")
            manifest=AnalysisInputManifest.model_validate_json(canonical_json_bytes(raw["manifest"]))
            if (manifest.job_id!=job_id or manifest.case_id!=task.case_id or manifest.id!=task.manifest_id
                    or manifest.input_checksum!=task.input_checksum or manifest.mode=="review"
                    or task.report_type!=("prospect" if manifest.mode=="acquisition" else "verified_execution")):
                raise ValueError("Invalid frozen binding")
            completed=None; value=None
            if raw["completed_report"] is not None:
                completed=CapabilityReport.model_validate_json(canonical_json_bytes(raw["completed_report"]))
                if raw["request"] is not None or raw["previous_report"] is not None or completed.input_manifest!=manifest:
                    raise ValueError("Invalid completed result")
            else:
                manifest,value=validate_generation_input(manifest,raw["request"],raw["previous_report"])
            frozen=FrozenCapabilityInput(manifest=manifest,request=value,previous_report=raw["previous_report"],completed_report=completed)
            _SEALS[frozen]=(run_generation,_digest(frozen))
            return frozen
        except (ValueError,TypeError,AttributeError,KeyError,OverflowError,RecursionError):
            raise self.rpc.invalid() from None
