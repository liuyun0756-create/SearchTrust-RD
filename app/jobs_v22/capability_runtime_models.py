"""Versioned internal request and minimal public completion reference.

Legacy API bundle is deliberately unchanged. This is not a paid intake model.
"""
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from app.report_v22.models import StrictModel


class CapabilityTaskRequest(StrictModel):
    case_id: UUID
    manifest_id: UUID
    input_checksum: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    report_type: Literal["prospect", "verified_execution"]


class CapabilityRequestEnvelope(StrictModel):
    schema_version: Literal["v22_capability_request_envelope_v1"]
    capability_request: CapabilityTaskRequest


class CapabilityReportReference(StrictModel):
    report_id: UUID
    case_id: UUID
    schema_version: Literal["2.2.2"]
    report_type: Literal["prospect", "verified_execution"]
    payload_checksum: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")


class CapabilityTaskStatusResponse(StrictModel):
    schema_version: Literal["v22_capability_task_status_v1"]
    job_id: UUID
    revision: int = Field(ge=1)
    run_generation: int = Field(ge=1)
    status: Literal["succeeded"]
    stage: Literal["completed"]
    progress: Literal[100]
    message: str = Field(min_length=1, max_length=500)
    report_reference: CapabilityReportReference
    created_at: AwareDatetime
    deadline_at: AwareDatetime
    updated_at: AwareDatetime

    @model_validator(mode="after")
    def bound_completion(self):
        if self.job_id!=self.report_reference.report_id or self.deadline_at<=self.created_at or self.updated_at<self.created_at:
            raise ValueError("Invalid completed task reference")
        return self
