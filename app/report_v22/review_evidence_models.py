"""Internal review contracts. Integrity is not ownership or persisted approval."""
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from app.report_v22.review_plan_basis import PlanReport, PlanDelivery
from app.jobs_v22.digest import canonical_json_bytes, verified_request_digest
from app.report_v22.capability import CapabilityReport, Checksum, DeliveryRevision, Label, Timestamp
from app.report_v22.comparison_base import StrictModel
from app.report_v22.comparison_storage import FrozenComparison
from app.report_v22.ga4_collection_storage import Ga4CollectionJob
from app.report_v22.implementation_records import ImplementationRecord
from app.report_v22.page_implementation_models import PageObservation

RULES = "v22_review_projection_rules_v1"
MAX_BYTES = 2_000_000
Reason = Annotated[str, Field(min_length=1, max_length=1200)]
Target = Annotated[str, Field(min_length=1, max_length=2000)]
Work = Annotated[str, Field(min_length=1, max_length=4000)]
Disposition = Literal["continue", "adjust", "observe", "defer", "new"]


class PageSelection(StrictModel):
    current: PageObservation
    previous: PageObservation | None


class ActionSelection(StrictModel):
    action_id: Label
    page: PageSelection | None
    gsc: FrozenComparison | None
    ga4: Ga4CollectionJob | None

    @field_validator("gsc", "ga4", mode="before")
    @classmethod
    def parse_nested(cls, value, info):
        if value is None:
            return None
        model = FrozenComparison if info.field_name == "gsc" else Ga4CollectionJob
        raw = value.model_dump(mode="json") if isinstance(value, model) else value
        return model.model_validate_json(canonical_json_bytes(raw))


class ReviewDecision(StrictModel):
    source: Literal["seo_recorded"]
    evidence_checksum: Checksum
    recorded_at: Timestamp
    action_id: Label
    disposition: Disposition
    reason: Reason
    references: list[Label] = Field(max_length=12)
    target: Target | None
    work: Work | None
    completion_statement: Reason | None
    reconsider_when: Reason | None

    @field_validator("reason", "target", "work", "completion_statement", "reconsider_when")
    @classmethod
    def plain_text(cls, value):
        if value is not None and (not value.strip() or any((ord(c) < 32 and c not in "\n\t") or ord(c) == 127 for c in value)):
            raise ValueError("nonempty plain text required")
        return value

    @model_validator(mode="after")
    def shape(self):
        if len(set(self.references)) != len(self.references):
            raise ValueError("duplicate decision references")
        edited = self.disposition in {"adjust", "new"}
        if edited != (self.target is not None and self.work is not None) or (not edited and (self.target is not None or self.work is not None)):
            raise ValueError("only adjusted/new decisions carry proposed work")
        if edited and not self.references:
            raise ValueError("proposed work requires traceable evidence")
        if (self.disposition == "observe") != (self.completion_statement is not None):
            raise ValueError("observation requires an explicit SEO completion statement")
        if (self.disposition == "defer") != (self.reconsider_when is not None):
            raise ValueError("deferral requires a reconsideration condition")
        return self


class ReviewEvidenceCore(StrictModel):
    user_id: UUID
    case_id: UUID
    evaluated_at: Timestamp
    rules_version: Literal["v22_review_projection_rules_v1"]
    report: PlanReport
    report_checksum: Checksum
    delivery_id: UUID
    delivery_revision: int = Field(ge=1, lt=2147483647)
    delivery: PlanDelivery
    delivery_checksum: Checksum
    implementation: ImplementationRecord
    selections: list[ActionSelection] = Field(max_length=3)


class ReviewEvidenceBundle(ReviewEvidenceCore):
    schema_version: Literal["v22_review_evidence_bundle_v1"]
    evidence_checksum: Checksum
    decisions: list[ReviewDecision] = Field(max_length=6)
    checksum: Checksum

    @model_validator(mode="after")
    def integrity(self):
        from app.report_v22.review_evidence_validation import validate_evidence, validate_decisions
        raw = self.model_dump(mode="json")
        if len(canonical_json_bytes(raw)) > MAX_BYTES:
            raise ValueError("review input exceeds limit")
        core = {k: v for k, v in raw.items() if k in ReviewEvidenceCore.model_fields}
        if (verified_request_digest(core) != self.evidence_checksum
                or verified_request_digest({k: v for k, v in raw.items() if k != "checksum"}) != self.checksum):
            raise ValueError("review checksum mismatch")
        validate_evidence(self)
        validate_decisions(self)
        return self
