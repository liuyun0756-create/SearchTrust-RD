"""Strict reference-only contracts for durable Verified analysis requests."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from app.report_v22.models import StrictModel


class VerifiedTaskRequest(StrictModel):
    schema_version: Literal["v22_verified_task_request_v1"] = "v22_verified_task_request_v1"
    case_id: UUID
    parent_report_id: UUID
    gsc_snapshot_id: UUID
    ga4_snapshot_id: UUID
    public_gbp_snapshot_id: UUID
    input_checksum: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_distinct_sources(self) -> "VerifiedTaskRequest":
        source_ids = (
            self.parent_report_id,
            self.gsc_snapshot_id,
            self.ga4_snapshot_id,
            self.public_gbp_snapshot_id,
        )
        if len(set(source_ids)) != len(source_ids):
            raise ValueError("Verified parent report and snapshot identities must be distinct")
        return self


class VerifiedRequestEnvelope(StrictModel):
    schema_version: Literal["v22_verified_request_envelope_v1"]
    verified_request: VerifiedTaskRequest
