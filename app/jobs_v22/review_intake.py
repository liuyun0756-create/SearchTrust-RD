"""Atomic paid review intake over explicitly selected, owner-scoped records."""
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from app.jobs_v22.digest import canonical_json_bytes
from app.jobs_v22.errors import DeterministicJobError
from app.jobs_v22.review_input_repository import ReviewInputRepository
from app.report_v22.capability import Checksum, Timestamp, instant
from app.report_v22.comparison_base import StrictModel
from app.report_v22.review_generation_models import ReviewGenerationSelection, checked
from app.report_v22.review_freeze_storage import decode

MAX_COMMAND = 8192
PREFIX = "V22_REVIEW_START_"


def invalid(code="INVALID"):
    return DeterministicJobError(PREFIX + code, "The saved review could not be started or checked.")


class StartReview(StrictModel):
    operation: Literal["start"]
    user_id: UUID
    case_id: UUID
    report_id: UUID
    selection: ReviewGenerationSelection
    confirmed: Literal[True]

    @model_validator(mode="before")
    @classmethod
    def explicit_confirmation(cls, value):
        if isinstance(value, dict) and value.get("confirmed") is not True:
            raise ValueError("explicit confirmation required")
        return value


class ReadReview(StrictModel):
    operation: Literal["status"]
    user_id: UUID
    case_id: UUID
    report_id: UUID
    job_id: UUID


class FindReview(StrictModel):
    operation: Literal["find"]
    user_id: UUID
    case_id: UUID
    report_id: UUID
    review_id: UUID
    decision_id: UUID


class ReviewJob(StrictModel):
    schema_version: Literal["v22_review_job_v1"]
    job_id: UUID
    user_id: UUID
    case_id: UUID
    base_report_id: UUID
    selection: ReviewGenerationSelection
    input_checksum: Checksum
    revision: int = Field(ge=1)
    run_generation: int = Field(ge=1)
    status: Literal["queued", "running", "succeeded", "failed"]
    created_at: Timestamp
    deadline_at: Timestamp
    updated_at: Timestamp
    report_id: UUID | None
    credit_state: Literal["reserved", "consumed", "compensated"]
    error_code: Literal["TECHNICAL_FAILURE"] | None

    @model_validator(mode="after")
    def lifecycle(self):
        expected = "consumed" if self.status == "succeeded" else "compensated" if self.status == "failed" else "reserved"
        if (self.selection.job_id != self.job_id or self.credit_state != expected
            or self.report_id != (self.job_id if self.status == "succeeded" else None)
            or self.error_code != ("TECHNICAL_FAILURE" if self.status == "failed" else None)
            or instant(self.deadline_at) <= instant(self.created_at)):
            raise ValueError("invalid review lifecycle")
        return self


def parse_command(raw):
    try:
        v = decode(raw) if isinstance(raw, (str, bytes, bytearray)) else raw
        model = {"start": StartReview, "status": ReadReview, "find": FindReview}.get(v.get("operation"))
        if model is None:
            raise ValueError()
        return checked(model, v, MAX_COMMAND)
    except (ValueError, TypeError, AttributeError, DeterministicJobError):
        raise invalid() from None


class ReviewIntakeRepository(ReviewInputRepository):
    RPC_NAMES = {"lookup_v22_review_generation", "start_v22_review_generation", "get_v22_review_generation_status", "find_v22_review_generation"}
    ERROR_CODES = {"REVIEW_START_" + c: c for c in ("INVALID", "UNAVAILABLE", "CONFLICT", "STALE", "BUSY", "INSUFFICIENT_CREDITS")}
    ERROR_CODES.update({"REVIEW_GENERATION_" + c: "INVALID" for c in ("INVALID", "TOO_LARGE")})
    input_error = staticmethod(invalid)
    response_limit = 16384

    async def run(self, name, args, command):
        raw = await self._rpc(name, args)
        if raw.strip() == b"null":
            return None
        try:
            r = checked(ReviewJob, raw, self.response_limit)
            if (r.user_id, r.case_id, r.base_report_id) != (command.user_id, command.case_id, command.report_id):
                raise invalid()
            if isinstance(command, StartReview) and r.selection != command.selection:
                raise invalid()
            if isinstance(command, ReadReview) and r.job_id != command.job_id:
                raise invalid()
            if isinstance(command, FindReview) and (r.selection.review_id, r.selection.decision_id) != (command.review_id, command.decision_id):
                raise invalid()
            return r
        except DeterministicJobError:
            # A successful write may have committed; do not invite a new debit.
            raise self.storage_error() from None


class ReviewIntakeService:
    def __init__(self, repository, preparer):
        self.repository, self.preparer = repository, preparer

    async def run(self, command):
        c = parse_command(command.model_dump(mode="json") if isinstance(command, StrictModel) else command)
        args = dict(p_user_id=str(c.user_id), p_case_id=str(c.case_id), p_report_id=str(c.report_id))
        if isinstance(c, ReadReview):
            return await self.repository.run("get_v22_review_generation_status", args | dict(p_job_id=str(c.job_id)), c)
        if isinstance(c, FindReview):
            return await self.repository.run("find_v22_review_generation", args | dict(p_review_id=str(c.review_id), p_decision_id=str(c.decision_id)), c)
        args |= dict(p_command_text=canonical_json_bytes(c.selection.model_dump(mode="json")).decode())
        old = await self.repository.run("lookup_v22_review_generation", args, c)
        if old is not None:
            return old
        value = await self.preparer.prepare(user_id=c.user_id, case_id=c.case_id, report_id=c.report_id, selection=c.selection)
        result = await self.repository.run("start_v22_review_generation", args | dict(p_input_text=canonical_json_bytes(value.model_dump(mode="json")).decode()), c)
        if result is None:
            raise self.repository.storage_error()
        return result
