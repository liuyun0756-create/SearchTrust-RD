"""Formal review completion and exact recovery over the existing one-credit ledger."""
from typing import Literal
from uuid import UUID

from pydantic import Field

from app.jobs_v22.digest import canonical_json_bytes, verified_request_digest
from app.jobs_v22.errors import DeterministicJobError, TransientJobError
from app.jobs_v22.review_input_repository import ReviewInputRepository, require_frozen_review_input
from app.report_v22.capability import Checksum
from app.report_v22.comparison_base import StrictModel
from app.report_v22.review_generation_models import MAX_INPUT, ReviewGenerationInput, checked
from app.report_v22.review_report import VerifiedReviewReport, build_verified_review_report, validate_verified_review_report


def invalid(code="INVALID"):
    return DeterministicJobError(f"V22_REVIEW_REPORT_{code}","The review report is unavailable or inconsistent with its saved input.")


def unknown():
    return TransientJobError("V22_REVIEW_REPORT_STORAGE_UNAVAILABLE",
        "The report completion could not be verified. Recover the same task before retrying.")


class ReviewCompletionReceipt(StrictModel):
    report_id: UUID
    payload_checksum: Checksum
    idempotent: bool


class CompletedReviewEnvelope(StrictModel):
    schema_version: Literal["v22_completed_review_report_v1"]
    report: VerifiedReviewReport
    payload_checksum: Checksum
    input_text: str = Field(max_length=MAX_INPUT)


class ReviewReportRepository(ReviewInputRepository):
    RPC_NAMES={"complete_v22_review_analysis","get_v22_completed_review_report"}
    ERROR_CODES={f"REVIEW_REPORT_{c}":c for c in ("INVALID","UNAVAILABLE","INACTIVE","CONFLICT","STALE","TOO_LARGE")}
    ERROR_CODES.update({f"REVIEW_GENERATION_{c}":c for c in ("INVALID","UNAVAILABLE","INACTIVE","CONFLICT","NOT_RESERVED")})
    response_limit=7_000_000
    storage_error=staticmethod(unknown)
    input_error=staticmethod(invalid)

    async def complete(self, *, frozen, report, run_generation):
        value=require_frozen_review_input(frozen,run_generation=run_generation)
        report=validate_verified_review_report(report,value)
        args=self.identity(user_id=value.user_id,case_id=value.case_id,job_id=value.job_id,run_generation=run_generation)
        raw=await self._rpc("complete_v22_review_analysis",args|dict(p_report_text=canonical_json_bytes(report.model_dump(mode="json")).decode()))
        try:
            receipt=checked(ReviewCompletionReceipt,raw,4096)
            if receipt.report_id!=report.id or receipt.payload_checksum!=verified_request_digest(report.model_dump(mode="json")):
                raise invalid()
        except DeterministicJobError:
            raise unknown() from None
        return receipt

    async def read(self, *, user_id,case_id,job_id):
        args=self.identity(user_id=user_id,case_id=case_id,job_id=job_id,run_generation=1)
        args.pop("p_run_generation")
        raw=await self._rpc("get_v22_completed_review_report",args)
        try:
            envelope=checked(CompletedReviewEnvelope,raw,self.response_limit)
            value=checked(ReviewGenerationInput,envelope.input_text,MAX_INPUT)
            report=validate_verified_review_report(envelope.report,value)
            if (str(report.id)!=args["p_job_id"] or str(value.user_id)!=args["p_user_id"]
                or str(value.case_id)!=args["p_case_id"] or envelope.payload_checksum!=verified_request_digest(report.model_dump(mode="json"))):
                raise invalid()
        except DeterministicJobError:
            raise unknown() from None
        return report


class ReviewCompletionService:
    def __init__(self, repository):
        self.repository=repository

    async def complete(self, *, frozen,run_generation,content=None):
        value=require_frozen_review_input(frozen,run_generation=run_generation)
        report=build_verified_review_report(value,content=content)
        receipt=await self.repository.complete(frozen=frozen,report=report,run_generation=run_generation)
        return report,receipt
