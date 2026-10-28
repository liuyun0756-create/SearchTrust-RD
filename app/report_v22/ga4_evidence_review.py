"""Independent SEO statements bound to immutable evidence, with revision history."""
import json
from datetime import datetime, timezone
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from app.jobs_v22.digest import verified_request_digest, verified_canonical_json_bytes
from app.jobs_v22.errors import DeterministicJobError
from app.report_v22.capability import Checksum, Timestamp, instant
from app.report_v22.comparison_base import StrictModel
from app.report_v22.ga4_collection_storage import Ga4CollectionRepository, invalid
from app.report_v22.ga4_evidence_audit import Ga4EvidenceAudit, audit_ga4


class SeoCheck(StrictModel):
    status: Literal["not_checked", "no_issue_found", "issue_found", "uncertain"]
    note: str = Field(max_length=1200)

    @model_validator(mode="after")
    def basis_required(self):
        if any((ord(c) < 32 and c not in "\n\t") or ord(c) == 127 for c in self.note) or (self.status != "not_checked" and not self.note.strip()):
            raise ValueError("checked states require a plain-text basis")
        return self


class SeoChecks(StrictModel):
    date_coverage: SeoCheck
    event_history: SeoCheck
    measurement_setup: SeoCheck


class Ga4ReviewInput(StrictModel):
    request_id: UUID
    expected_revision: int = Field(ge=0, le=2147483645)
    observation_checksum: Checksum
    checks: SeoChecks


class Ga4ReviewRecord(StrictModel):
    schema_version: Literal["v22_ga4_seo_review_v1"]
    source: Literal["seo_recorded"]
    user_id: UUID
    case_id: UUID
    report_id: UUID
    delivery_revision_id: UUID
    collection_id: UUID
    request_id: UUID
    observation_checksum: Checksum
    revision: int = Field(ge=1, le=2147483646)
    recorded_at: Timestamp
    checks: SeoChecks
    checksum: Checksum

    @model_validator(mode="after")
    def integrity(self):
        if self.checksum != verified_request_digest(self.model_dump(mode="json", exclude={"checksum"})):
            raise ValueError("review checksum mismatch")
        return self


class Ga4ReviewView(StrictModel):
    schema_version: Literal["v22_ga4_review_view_v1"]
    case_id: UUID
    report_id: UUID
    delivery_revision_id: UUID
    audit: Ga4EvidenceAudit
    reviews: list[Ga4ReviewRecord] = Field(max_length=20)
    # Idempotent receipt may be older than the latest review, but is never substituted for it.
    saved: Ga4ReviewRecord | None


class Ga4ReviewCommand(StrictModel):
    operation: Literal["read", "save"]
    user_id: UUID
    case_id: UUID
    report_id: UUID
    delivery_revision_id: UUID
    collection_id: UUID
    review: Ga4ReviewInput | None

    @model_validator(mode="after")
    def shape(self):
        if (self.operation == "save") != (self.review is not None):
            raise ValueError("invalid review command")
        return self


class Ga4ReviewContracts(StrictModel):
    view: Ga4ReviewView
    input: Ga4ReviewInput


class Ga4EvidenceReview:
    def __init__(self, repository: Ga4CollectionRepository, *, clock=None):
        self.repository = repository
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    async def run(self, command: Ga4ReviewCommand):
        c = Ga4ReviewCommand.model_validate_json(command.model_dump_json())
        job = await self.repository.read_collection(user_id=c.user_id, case_id=c.case_id, collection_id=c.collection_id)
        if not job or (job.record.report_id, job.record.delivery_revision_id) != (c.report_id, c.delivery_revision_id):
            raise DeterministicJobError("V22_GA4_REVIEW_NOT_FOUND", "This collection is not available for this report.")
        if job.status != "succeeded":
            raise DeterministicJobError("V22_GA4_REVIEW_CONFLICT", "Only completed observations can be reviewed.")
        audit = audit_ga4(job.id, job.collection.snapshot)
        args = dict(p_user_id=str(c.user_id), p_case_id=str(c.case_id), p_report_id=str(c.report_id),
            p_delivery_revision_id=str(c.delivery_revision_id), p_collection_id=str(c.collection_id))
        saved = None
        if c.review:
            if c.review.observation_checksum != audit.observation_checksum:
                raise DeterministicJobError("V22_GA4_REVIEW_CONFLICT", "This review belongs to different evidence.")
            now = self.clock()
            if now.tzinfo is None:
                raise invalid()
            body = dict(schema_version="v22_ga4_seo_review_v1", source="seo_recorded", user_id=str(c.user_id),
                case_id=str(c.case_id), report_id=str(c.report_id), delivery_revision_id=str(c.delivery_revision_id),
                collection_id=str(c.collection_id), request_id=str(c.review.request_id),
                observation_checksum=audit.observation_checksum, revision=c.review.expected_revision+1,
                recorded_at=now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"), checks=c.review.checks.model_dump(mode="json"))
            payload = body | {"checksum": verified_request_digest(body)}
            raw = await self.repository._rpc("save_v22_ga4_evidence_review", args | dict(p_expected_revision=c.review.expected_revision,
                p_payload=payload, p_canonical_content=verified_canonical_json_bytes(body).decode()))
            saved = self._record(raw, c, audit)
            if (saved.request_id, saved.revision, saved.checks) != (c.review.request_id, c.review.expected_revision+1, c.review.checks):
                raise invalid()
        raw = await self.repository._rpc("get_v22_ga4_evidence_reviews", args)
        try:
            values = json.loads(raw)
            if not isinstance(values, list) or len(values) > 20:
                raise ValueError("invalid review history")
            reviews = [self._record(json.dumps(r), c, audit) for r in values]
            if reviews and [r.revision for r in reviews] != list(range(reviews[0].revision, max(0, reviews[0].revision-20), -1)):
                raise ValueError("non-contiguous history")
            if len({r.request_id for r in reviews}) != len(reviews) or any(instant(reviews[i].recorded_at) < instant(reviews[i+1].recorded_at) for i in range(len(reviews)-1)):
                raise ValueError("inconsistent review history")
            if saved and (not reviews or saved.revision > reviews[0].revision or (saved.revision >= reviews[-1].revision and saved not in reviews)):
                raise ValueError("missing save receipt")
        except (ValueError, TypeError):
            raise invalid() from None
        return Ga4ReviewView(schema_version="v22_ga4_review_view_v1", case_id=c.case_id, report_id=c.report_id,
            delivery_revision_id=c.delivery_revision_id, audit=audit, reviews=reviews, saved=saved)

    @staticmethod
    def _record(raw, c, audit):
        try:
            r = Ga4ReviewRecord.model_validate_json(raw)
            if (r.user_id, r.case_id, r.report_id, r.delivery_revision_id, r.collection_id, r.observation_checksum) != (
                c.user_id, c.case_id, c.report_id, c.delivery_revision_id, c.collection_id, audit.observation_checksum):
                raise ValueError("review evidence binding mismatch")
            return r
        except (ValueError, TypeError):
            raise invalid() from None
