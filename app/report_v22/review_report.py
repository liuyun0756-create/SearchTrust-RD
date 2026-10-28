"""Formal Verified review version; client delivery approval remains separate."""
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from app.report_v22.review_plan_basis import plan_business
from app.jobs_v22.digest import verified_request_digest
from app.report_v22.capability import BusinessScope, Checksum, EditableAction, Label, Text, Timestamp, instant
from app.report_v22.comparison_base import StrictModel
from app.report_v22.review_generation_content import ReviewGenerationContent, build_review_generation_content
from app.report_v22.review_generation_models import MAX_INPUT, ReviewGenerationInput, ReviewGenerationRecipe, checked, invalid, recipe_for
from app.report_v22.review_evidence_models import Work

MAX_REPORT = 1_300_000
TITLE = "Verified Action Plan · Review"
COMPLETION = "Confirm the agreed work is live and record the implementation date."
REVIEW = "Review the same target and eligible evidence after implementation."
LIMIT = "Observed changes do not establish that this work caused a business result."


class ReviewPlanAction(EditableAction):
    steps: list[Work] = Field(min_length=1,max_length=12)


class VerifiedReviewReport(StrictModel):
    schema_version: Literal["2.2.3"]
    id: UUID
    report_type: Literal["verified_execution"]
    version: int = Field(ge=2, lt=2147483647)
    generated_at: Timestamp
    input_reference: ReviewGenerationRecipe
    business: BusinessScope
    title: Label
    summary: Text
    content: ReviewGenerationContent
    action_state: Literal["actions_supported", "no_next_actions"]
    actions: list[ReviewPlanAction] = Field(max_length=3)
    limitations: list[Text] = Field(min_length=1, max_length=40)
    delivery_state: Literal["requires_delivery_review"]
    settlement_policy: Literal["one_credit_per_completed_analysis"]
    checksum: Checksum

    @model_validator(mode="after")
    def integrity(self):
        r, c = self.input_reference, self.content
        if (self.id,self.business.user_id,self.business.case_id)!=(r.job_id,r.user_id,r.case_id):
            raise ValueError("review report identity mismatch")
        if (c.job_id,c.user_id,c.case_id,c.input_checksum,c.decision_id,c.review_id)!=(r.job_id,r.user_id,r.case_id,r.input_checksum,r.decision_id,r.review_id):
            raise ValueError("review content mismatch")
        if self.version!=c.proposed_version or self.generated_at!=r.prepared_at or c.prepared_at!=self.generated_at:
            raise ValueError("review time/version mismatch")
        instant(self.generated_at)
        if bool(self.actions)!=(self.action_state=="actions_supported") or len({a.id for a in self.actions})!=len(self.actions):
            raise ValueError("invalid next plan")
        if self.checksum!=verified_request_digest(self.model_dump(mode="json",exclude={"checksum"})):
            raise ValueError("review report checksum mismatch")
        return self


def _next_actions(value):
    original={a.id:a for a in value.evidence.bundle.delivery.actions}
    actions=[]
    for decision in value.decision.decisions:
        if decision.disposition=="continue":
            actions.append(original[decision.action_id].model_dump(mode="json"))
        elif decision.disposition in {"adjust","new"}:
            work=decision.work
            actions.append(dict(id=decision.action_id,
                title=original[decision.action_id].title if decision.disposition=="adjust" else "New work proposed by SEO",
                target=decision.target,steps=[work],
                completion=[COMPLETION],review=REVIEW,materials=[]))
    return actions


def build_verified_review_report(value, *, content=None):
    value=checked(ReviewGenerationInput,value,MAX_INPUT)
    expected=build_review_generation_content(value)
    if content is not None and checked(ReviewGenerationContent,content,1_000_000)!=expected:
        raise invalid("CONTENT_INVALID")
    actions=_next_actions(value)
    raw=dict(schema_version="2.2.3",id=str(value.job_id),report_type="verified_execution",
        version=expected.proposed_version,generated_at=value.prepared_at,input_reference=recipe_for(value).model_dump(mode="json"),
        business=plan_business(value.evidence.bundle.report).model_dump(mode="json"),title=TITLE,
        summary="Review the recorded work, the selected evidence and the next actions before client delivery.",
        content=expected.model_dump(mode="json"),action_state="actions_supported" if actions else "no_next_actions",actions=actions,
        limitations=list(dict.fromkeys([LIMIT,*value.evidence.bundle.report.limitations]))[:40],
        delivery_state="requires_delivery_review",settlement_policy="one_credit_per_completed_analysis")
    return checked(VerifiedReviewReport,raw|dict(checksum=verified_request_digest(raw)),MAX_REPORT)


def validate_verified_review_report(report,value):
    report=checked(VerifiedReviewReport,report,MAX_REPORT)
    if report!=build_verified_review_report(value):
        raise invalid("REPORT_INVALID")
    return report
