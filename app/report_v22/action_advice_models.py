"""Contract for frozen advice attached to an existing action."""
from typing import Annotated, Literal
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator

from app.report_v22.models import StrictModel

Line = Annotated[str, Field(min_length=1, max_length=2000, pattern=r"\S")]
CopyText = Annotated[str, Field(min_length=1, max_length=700, pattern=r"\S")]
AdviceError = Literal["not_attempted", "no_approved_copy", "provider_unavailable", "timeout", "invalid_response",
                      "unsupported_copy", "restricted_claim", "binding_changed", "target_scope_unverified"]


class AdviceReference(StrictModel):
    model_config = ConfigDict(str_strip_whitespace=False)
    text: CopyText
    url: Annotated[str, Field(max_length=2083, pattern=r"^https?://[^\s]+$")]
    snapshot_id: UUID
    path: Annotated[str, Field(pattern=r"^/payload/selected_pages/\d+/deep_snapshot/text$")]


class AdviceExample(StrictModel):
    model_config = ConfigDict(str_strip_whitespace=False)
    text: CopyText
    version: Literal["action_example_policy_v1"] = "action_example_policy_v1"
    template: Literal["contact", "discuss"]
    evidence_ids: list[Line] = Field(min_length=1, max_length=50)


class ActionAdvice(StrictModel):
    model_config = ConfigDict(str_strip_whitespace=False)
    action_id: Line
    problem: list[Line] = Field(min_length=1, max_length=20)
    evidence_ids: list[Line] = Field(min_length=1, max_length=50)
    why_important: list[Line] = Field(min_length=1, max_length=20)
    fix: list[Line] = Field(min_length=1, max_length=12)
    where: Line
    priority: int | None = Field(ge=1, le=3)
    effort: Literal["small", "medium", "large", "not_assessed"]
    confidence: Literal["low", "medium", "high"]
    verification: list[Line] = Field(min_length=1, max_length=12)
    reference: AdviceReference | None = None
    example: AdviceExample | None = None
    rejection_reason: AdviceError | None = None

    @model_validator(mode="after")
    def coherent_copy(self):
        if sum(value is not None for value in (self.reference, self.example, self.rejection_reason)) != 1:
            raise ValueError("action advice must have one copy outcome")
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("duplicate advice evidence")
        if self.example and self.example.evidence_ids != self.evidence_ids:
            raise ValueError("example binding mismatch")
        if self.priority is None and self.example:
            raise ValueError("only selected advice can contain a generated example")
        return self
