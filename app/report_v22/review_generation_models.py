"""Internal generation inputs, not a paid task or a completed report contract."""
from typing import Literal
from uuid import UUID

from pydantic import model_validator

from app.jobs_v22.digest import verified_request_digest
from app.jobs_v22.errors import DeterministicJobError
from app.report_v22.capability import Checksum, Timestamp, instant
from app.report_v22.comparison_base import StrictModel
from app.report_v22.review_decision_models import DecisionRevision
from app.report_v22.review_decision_validation import parse, validate_revision
from app.report_v22.review_freeze_models import SavedReviewEvidence

MAX_INPUT = 2_300_000
MAX_REFERENCE = 4_096
MAX_CONTENT = 1_000_000
MAX_CHECKPOINT = 1_100_000
RULES = "v22_review_generation_rules_v1"


def invalid(code="INVALID"):
    return DeterministicJobError(f"V22_REVIEW_GENERATION_{code}",
        "The selected review generation material is unavailable or inconsistent.")


def checked(model, value, limit):
    # Re-serialize model instances too: callers can mutate Pydantic objects.
    try:
        return parse(model, value, limit)
    except DeterministicJobError as exc:
        raise invalid("TOO_LARGE" if exc.error_code.endswith("TOO_LARGE") else "INVALID") from None


class ReviewGenerationSelection(StrictModel):
    job_id: UUID
    review_id: UUID
    review_checksum: Checksum
    decision_id: UUID
    decision_checksum: Checksum


class ReviewGenerationInput(StrictModel):
    schema_version: Literal["v22_review_generation_input_v1"]
    rules_version: Literal["v22_review_generation_rules_v1"]
    job_id: UUID
    user_id: UUID
    case_id: UUID
    report_id: UUID
    prepared_at: Timestamp
    evidence: SavedReviewEvidence
    decision: DecisionRevision
    checksum: Checksum

    @model_validator(mode="after")
    def binding(self):
        saved = self.evidence
        report = saved.bundle.report
        if (self.user_id, self.case_id, self.report_id) != (saved.user_id, saved.case_id, report.id):
            raise ValueError("input identity mismatch")
        if report.report_type != "verified_execution" or self.job_id == report.id:
            raise ValueError("requires a new generation over a Verified report")
        validate_revision(self.decision, saved, user_id=self.user_id, case_id=self.case_id, report_id=self.report_id)
        if instant(self.prepared_at) < max(instant(saved.frozen_at), instant(saved.bundle.evaluated_at), instant(self.decision.recorded_at)):
            raise ValueError("generation predates its input")
        if self.checksum != verified_request_digest(self.model_dump(mode="json", exclude={"checksum"})):
            raise ValueError("input checksum mismatch")
        return self


class ReviewGenerationRecipe(ReviewGenerationSelection):
    schema_version: Literal["v22_review_generation_recipe_v1"]
    rules_version: Literal["v22_review_generation_rules_v1"]
    user_id: UUID
    case_id: UUID
    report_id: UUID
    prepared_at: Timestamp
    input_checksum: Checksum


def recipe_for(value):
    value = checked(ReviewGenerationInput, value, MAX_INPUT)
    raw = dict(schema_version="v22_review_generation_recipe_v1", rules_version=RULES,
        **value.model_dump(mode="json", include={"job_id", "user_id", "case_id", "report_id", "prepared_at"}),
        review_id=str(value.evidence.id), review_checksum=value.evidence.bundle.checksum,
        decision_id=str(value.decision.id), decision_checksum=value.decision.checksum, input_checksum=value.checksum)
    return checked(ReviewGenerationRecipe, raw, MAX_REFERENCE)
