"""Independent SEO statements: never mutate or re-date frozen evidence."""
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from app.jobs_v22.digest import verified_request_digest
from app.report_v22.capability import Checksum, Label, Timestamp, instant
from app.report_v22.comparison_base import StrictModel
from app.report_v22.review_evidence_models import Disposition, Reason, Target, Work

MAX_INPUT = 65_536
MAX_RECORD = 131_072
MAX_HISTORY = 32_768
RevisionNumber = Annotated[int, Field(ge=1, lt=2147483647)]


class DecisionItem(StrictModel):
    source: Literal["seo_recorded"]
    action_id: Label
    disposition: Disposition
    reason: Reason
    references: list[Label] = Field(max_length=12)
    target: Target | None
    work: Work | None
    completion_statement: Reason | None
    reconsider_when: Reason | None

    @field_validator("action_id", "reason", "target", "work", "completion_statement", "reconsider_when")
    @classmethod
    def text(cls, value):
        if value is not None and (not value.strip() or any((ord(c) < 32 and c not in "\n\t") or ord(c) == 127 for c in value)):
            raise ValueError("nonempty plain text required")
        return value

    @model_validator(mode="after")
    def shape(self):
        if len(set(self.references)) != len(self.references):
            raise ValueError("duplicate references")
        edited = self.disposition in {"adjust", "new"}
        if edited != (self.target is not None and self.work is not None) or (not edited and (self.target is not None or self.work is not None)):
            raise ValueError("only adjusted/new work carries target and work")
        if edited and not self.references:
            raise ValueError("proposed work requires evidence")
        if (self.disposition == "observe") != (self.completion_statement is not None):
            raise ValueError("observation requires completion statement")
        if (self.disposition == "defer") != (self.reconsider_when is not None):
            raise ValueError("deferral requires reconsideration condition")
        return self


class SaveDecision(StrictModel):
    id: UUID
    review_id: UUID
    previous_id: UUID | None
    previous_checksum: Checksum | None
    decisions: list[DecisionItem] = Field(max_length=6)

    @model_validator(mode="after")
    def shape(self):
        if (self.previous_id is None) != (self.previous_checksum is None) or self.previous_id == self.id:
            raise ValueError("invalid predecessor")
        if len({d.action_id for d in self.decisions}) != len(self.decisions) or sum(d.disposition != "defer" for d in self.decisions) > 3:
            raise ValueError("duplicate or excessive decisions")
        return self


class DecisionRevision(SaveDecision):
    schema_version: Literal["v22_review_decision_revision_v1"]
    user_id: UUID
    case_id: UUID
    report_id: UUID
    evidence_checksum: Checksum
    bundle_checksum: Checksum
    revision: RevisionNumber
    recorded_at: Timestamp
    request_checksum: Checksum
    checksum: Checksum

    def request(self):
        return SaveDecision.model_validate_json(self.model_dump_json(include=set(SaveDecision.model_fields)))

    @model_validator(mode="after")
    def integrity(self):
        instant(self.recorded_at)
        if (self.revision == 1) != (self.previous_id is None):
            raise ValueError("invalid first revision")
        raw = self.model_dump(mode="json")
        if self.request_checksum != verified_request_digest(self.request().model_dump(mode="json")) or self.checksum != verified_request_digest({k: v for k, v in raw.items() if k != "checksum"}):
            raise ValueError("decision checksum mismatch")
        return self


class HistoryItem(StrictModel):
    id: UUID
    revision: RevisionNumber
    previous_id: UUID | None
    recorded_at: Timestamp
    checksum: Checksum
    decision_count: int = Field(ge=0, le=6)

    @model_validator(mode="after")
    def shape(self):
        instant(self.recorded_at)
        if (self.revision == 1) != (self.previous_id is None) or self.previous_id == self.id:
            raise ValueError("invalid predecessor")
        return self


class DecisionHistory(StrictModel):
    review_id: UUID
    report_id: UUID
    items: list[HistoryItem] = Field(max_length=20)
    next_cursor: RevisionNumber | None

    @model_validator(mode="after")
    def page(self):
        if len({i.id for i in self.items}) != len(self.items):
            raise ValueError("duplicate history")
        if self.items and len(self.items) < 20 and self.items[-1].revision != 1:
            raise ValueError("truncated history without continuation")
        for a, b in zip(self.items, self.items[1:]):
            if a.revision != b.revision + 1 or a.previous_id != b.id:
                raise ValueError("broken history chain")
        expected = self.items[-1].revision if len(self.items) == 20 and self.items[-1].revision > 1 else None
        if self.next_cursor != expected:
            raise ValueError("invalid history cursor")
        return self
