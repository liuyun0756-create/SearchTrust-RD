"""Dispatch authorization for prepared tasks, with no credit mutation."""
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator, model_validator
from app.jobs_v22.cost_models import validate_cost_counters_snapshot

from app.jobs_v22.capability_runtime_models import (
    CapabilityRequestEnvelope,
    CapabilityTaskStatusResponse,
)
from app.jobs_v22.digest import canonical_json_bytes
from app.report_v22.models import StrictModel


class CapabilityDispatch(StrictModel):
    schema_version: Literal["v22_capability_dispatch_v1"]
    user_id: UUID
    job_id: UUID
    case_id: UUID
    idempotency_key: str = Field(min_length=8, max_length=200, pattern=r"^[A-Za-z0-9._:-]+$")
    request: CapabilityRequestEnvelope
    run_generation: int = Field(ge=1)
    revision: int = Field(ge=1)
    attempt_count: int = Field(ge=0)
    cost_counters: dict[str, int | float]
    status: Literal["queued", "running", "succeeded"]
    created_at: AwareDatetime
    deadline_at: AwareDatetime
    completion: CapabilityTaskStatusResponse | None

    @field_validator("cost_counters")
    @classmethod
    def bounded_costs(cls, value):
        return validate_cost_counters_snapshot(value)

    @model_validator(mode="after")
    def bound(self):
        if self.request.capability_request.case_id != self.case_id or self.deadline_at <= self.created_at:
            raise ValueError("Invalid dispatch binding")
        if (self.status == "succeeded") != (self.completion is not None):
            raise ValueError("Invalid dispatch lifecycle")
        if self.completion is not None:
            c = self.completion
            if (c.job_id != self.job_id or c.report_reference.case_id != self.case_id
                    or c.report_reference.report_type != self.request.capability_request.report_type
                    or c.run_generation != self.run_generation or c.created_at != self.created_at
                    or c.deadline_at != self.deadline_at):
                raise ValueError("Invalid completed dispatch")
        return self


async def authorize_dispatch(repository, *, user_id, case_id, job_id, idempotency_key):
    raw = await repository.rpc.post("get_v22_capability_dispatch", dict(
        p_user_id=str(user_id), p_case_id=str(case_id), p_job_id=str(job_id),
        p_idempotency_key=idempotency_key,
    ))
    try:
        value = CapabilityDispatch.model_validate_json(canonical_json_bytes(raw))
        if (value.user_id != user_id or value.case_id != case_id
                or value.job_id != job_id or value.idempotency_key != idempotency_key):
            raise ValueError("Invalid dispatch identity")
        return value
    except (ValueError, TypeError, AttributeError):
        raise repository.rpc.invalid() from None
