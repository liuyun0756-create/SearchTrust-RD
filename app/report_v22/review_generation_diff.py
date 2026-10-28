"""Separate changed evidence from changed SEO work; provenance is not improvement."""
from app.report_v22.review_generation_models import MAX_INPUT, ReviewGenerationInput, checked, invalid
from app.report_v22.review_projection_diff import compare_review_projections


def _semantic(decision):
    return decision.model_dump(mode="json", exclude={"references"}) if decision else None


def compare_review_generation_inputs(current, previous=None):
    now = checked(ReviewGenerationInput, current, MAX_INPUT)
    old = checked(ReviewGenerationInput, previous, MAX_INPUT) if previous is not None else None
    if old and (now.user_id, now.case_id) != (old.user_id, old.case_id):
        raise invalid()
    difference = compare_review_projections(now.evidence.bundle, old.evidence.bundle if old else None)
    difference.current_input_checksum = now.checksum
    difference.previous_input_checksum = old.checksum if old else None
    if difference.state != "compared":
        return difference
    current_decisions = {d.action_id: d for d in now.decision.decisions}
    previous_decisions = {d.action_id: d for d in old.decision.decisions}
    for action in difference.actions:
        action.decision_changed = _semantic(current_decisions.get(action.action_id)) != _semantic(previous_decisions.get(action.action_id))
        if action.decision_changed:
            action.changed_sections.append("decision")
    difference.new_decisions_changed = (
        [_semantic(d) for d in now.decision.decisions if d.disposition == "new"] !=
        [_semantic(d) for d in old.decision.decisions if d.disposition == "new"])
    return difference
