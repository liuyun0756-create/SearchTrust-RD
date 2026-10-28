"""Internal reference-only selection and immutable, owner-bound review storage."""
from datetime import timedelta
from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from app.jobs_v22.digest import canonical_json_bytes, verified_request_digest
from app.jobs_v22.errors import DeterministicJobError
from app.report_v22.capability import Checksum, ImplementationReference, Label, Timestamp, instant
from app.report_v22.comparison_base import StrictModel
from app.report_v22.review_evidence_models import ReviewEvidenceBundle, ReviewEvidenceCore
from app.report_v22.review_projection import parse_review_bundle

MAX_SELECTION_BYTES = 32_768


class ReportReference(StrictModel):
    id: UUID
    checksum: Checksum


class ReviewSourceSelection(StrictModel):
    action_id: Label
    page_current_id: UUID | None
    page_previous_id: UUID | None
    gsc_id: UUID | None
    ga4_id: UUID | None

    @model_validator(mode="after")
    def page_pair(self):
        if self.page_previous_id and not self.page_current_id:
            raise ValueError("previous requires current page check")
        return self


class ReviewFreezeSelection(StrictModel):
    report: ReportReference
    delivery: ImplementationReference
    implementation: ImplementationReference
    actions: list[ReviewSourceSelection] = Field(max_length=3)

    @model_validator(mode="after")
    def unique(self):
        if len({s.action_id for s in self.actions}) != len(self.actions):
            raise ValueError("duplicate action")
        return self

    def check_bundle(self, bundle):
        b = bundle
        expected = dict(report=dict(id=str(b.report.id), checksum=b.report_checksum),
            delivery=dict(id=str(b.delivery_id), revision=b.delivery_revision, checksum=b.delivery_checksum),
            implementation=dict(id=str(b.implementation.id), revision=b.implementation.revision, checksum=b.implementation.checksum),
            actions=[dict(action_id=s.action_id, page_current_id=str(s.page.current.id) if s.page else None,
                page_previous_id=str(s.page.previous.id) if s.page and s.page.previous else None,
                gsc_id=str(s.gsc.id) if s.gsc else None, ga4_id=str(s.ga4.id) if s.ga4 else None) for s in b.selections])
        if self.model_dump(mode="json") != expected or b.decisions:
            raise ValueError("selection/source binding mismatch")


def checked_selection(value):
    try:
        raw = canonical_json_bytes(value)
        if len(raw) > MAX_SELECTION_BYTES:
            raise DeterministicJobError("V22_REVIEW_TOO_LARGE", "The review selection exceeds the supported size.")
        return ReviewFreezeSelection.model_validate_json(raw)
    except DeterministicJobError:
        raise
    except (ValueError, TypeError, OverflowError, RecursionError):
        raise invalid() from None


def invalid():
    return DeterministicJobError("V22_REVIEW_INVALID", "The selected review evidence is inconsistent or unavailable.")


def assemble_bundle(core):
    # Keep the legacy snapshot number representation; only hashes use Verified canonicalization.
    raw = core.model_dump(mode="json") if isinstance(core, ReviewEvidenceCore) else core
    raw = dict(raw, schema_version="v22_review_evidence_bundle_v1",
        evidence_checksum=verified_request_digest(raw), decisions=[])
    raw["checksum"] = verified_request_digest(raw)
    return parse_review_bundle(raw)


class ResolvedReview(StrictModel):
    case_updated_at: Timestamp
    bundle: ReviewEvidenceBundle

    @field_validator("bundle", mode="before")
    @classmethod
    def checked_bundle(cls, value):
        return parse_review_bundle(value)


class SavedReviewEvidence(StrictModel):
    schema_version: Literal["v22_saved_review_evidence_v1"]
    id: UUID
    user_id: UUID
    case_id: UUID
    selection: ReviewFreezeSelection
    bundle: ReviewEvidenceBundle
    frozen_at: Timestamp

    @field_validator("bundle", mode="before")
    @classmethod
    def checked_bundle(cls, value):
        return parse_review_bundle(value)

    @model_validator(mode="after")
    def bound(self):
        b = self.bundle
        if (self.user_id, self.case_id) != (b.user_id, b.case_id):
            raise ValueError("saved identity mismatch")
        self.selection.check_bundle(b)
        if instant(b.evaluated_at) > instant(self.frozen_at) + timedelta(minutes=1):
            raise ValueError("evaluation after save")
        return self
