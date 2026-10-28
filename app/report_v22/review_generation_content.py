"""Deterministic review content candidate; private decisions require delivery review."""
from app.report_v22.review_plan_basis import PlanAction
from typing import Literal
from uuid import UUID

from pydantic import Field

from app.jobs_v22.digest import verified_request_digest
from app.report_v22.capability import Checksum, EditableAction, Label, Timestamp
from app.report_v22.comparison_base import StrictModel
from app.report_v22.review_decision_models import DecisionItem
from app.report_v22.review_generation_models import MAX_CONTENT, MAX_INPUT, RULES, ReviewGenerationInput, checked, invalid
from app.report_v22.review_projection import build_review_projection, decision_summary
from app.report_v22.review_projection_models import ActionFacts, ClientAction, DecisionSummary, PriorDiagnosis


class GenerationSeoAction(StrictModel):
    facts: ActionFacts
    approved_action: PlanAction
    source_references: list[Label]
    selected_decision: DecisionItem | None


class ReviewGenerationContent(StrictModel):
    schema_version: Literal["v22_review_generation_content_v1"]
    rules_version: Literal["v22_review_generation_rules_v1"]
    job_id: UUID
    user_id: UUID
    case_id: UUID
    base_report_id: UUID
    report_type: Literal["verified_execution"]
    proposed_version: int = Field(ge=2)
    prepared_at: Timestamp
    input_checksum: Checksum
    review_id: UUID
    review_checksum: Checksum
    decision_id: UUID
    decision_checksum: Checksum
    delivery_state: Literal["requires_delivery_review"]
    prior_diagnosis: PriorDiagnosis
    seo_actions: list[GenerationSeoAction] = Field(max_length=3)
    seo_new_decisions: list[DecisionItem] = Field(max_length=3)
    client_actions: list[ClientAction] = Field(max_length=3)
    client_new_decisions: list[DecisionSummary] = Field(max_length=3)
    effect: None = None
    causal_attribution: Literal[False] = False
    checksum: Checksum


def _assemble(value):
    projection = build_review_projection(value.evidence.bundle)
    decisions = {d.action_id: d for d in value.decision.decisions}
    seo, client = [], []
    for action in projection.seo_actions:
        selected = decisions.get(action.facts.action_id)
        seo.append(GenerationSeoAction(facts=action.facts, approved_action=action.approved_action,
            source_references=action.source_references, selected_decision=selected).model_dump(mode="json"))
        # Never forward the raw reason, proposed target/work, completion claim,
        # or reconsideration condition to the client before delivery review.
        client.append(ClientAction(facts=action.facts,
            next_decision=decision_summary(action.facts.action_id, selected)).model_dump(mode="json"))
    new = [d for d in value.decision.decisions if d.disposition == "new"]
    raw = dict(schema_version="v22_review_generation_content_v1", rules_version=RULES,
        **value.model_dump(mode="json", include={"job_id", "user_id", "case_id", "prepared_at"}),
        base_report_id=str(value.report_id), report_type="verified_execution",
        proposed_version=value.evidence.bundle.report.version + 1, input_checksum=value.checksum,
        review_id=str(value.evidence.id), review_checksum=value.evidence.bundle.checksum,
        decision_id=str(value.decision.id), decision_checksum=value.decision.checksum,
        delivery_state="requires_delivery_review", prior_diagnosis=projection.prior_diagnosis.model_dump(mode="json"),
        seo_actions=seo, seo_new_decisions=[d.model_dump(mode="json") for d in new], client_actions=client,
        client_new_decisions=[decision_summary(d.action_id, d).model_dump(mode="json") for d in new],
        effect=None, causal_attribution=False)
    return checked(ReviewGenerationContent, raw | {"checksum": verified_request_digest(raw)}, MAX_CONTENT)


def build_review_generation_content(value):
    return _assemble(checked(ReviewGenerationInput, value, MAX_INPUT))


def validate_review_generation_content(content, value):
    value = checked(ReviewGenerationInput, value, MAX_INPUT)
    content = checked(ReviewGenerationContent, content, MAX_CONTENT)
    # A self-consistent hash alone cannot validate claims or privacy boundaries.
    # Pure deterministic verification does not fetch or regenerate source data.
    if content != _assemble(value):
        raise invalid("CONTENT_INVALID")
    return content
