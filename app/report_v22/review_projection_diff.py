"""Semantic comparisons rebuilt from validated inputs, never caller-made projections."""
from app.report_v22.review_projection import build_review_projection
from app.report_v22.review_projection_models import ActionDifference, ReviewDifference


def _decision(d):
    if d is None:
        return None
    # IDs/times of repeated evidence are provenance, not a changed work decision.
    return d.model_dump(mode="json", exclude={"evidence_checksum", "recorded_at", "references"})


def _sections(action):
    facts = action.facts.model_dump(mode="json")
    for field, omitted in (("page", {"observed_at", "previous_observed_at"}),
                           ("gsc", {"evaluated_at"}), ("ga4", {"started_at", "completed_at"})):
        if facts[field] is not None:
            facts[field] = {k: v for k, v in facts[field].items() if k not in omitted}
    return dict(approved_work=action.approved_action.model_dump(mode="json"),
        recorded_work=action.implementation_entry.model_dump(mode="json") if action.implementation_entry else None,
        page=facts["page"], gsc=facts["gsc"], ga4=facts["ga4"], checks=facts["checks"], decision=_decision(action.decision_record))


def compare_review_projections(current, previous=None):
    now = build_review_projection(current)
    old = build_review_projection(previous) if previous is not None else None
    base = dict(schema_version="v22_review_difference_v1", current_input_checksum=now.input_checksum,
        previous_input_checksum=old.input_checksum if old else None, actions=[], new_decisions_changed=False)
    if old is None:
        return ReviewDifference(state="first_baseline", reason="no_previous_projection", **base)
    basis = ("user_id", "case_id", "report_id", "report_checksum", "delivery_id", "delivery_revision", "delivery_checksum", "rules_version")
    if any(getattr(now, k) != getattr(old, k) for k in basis):
        return ReviewDifference(state="not_comparable", reason="different_review_basis", **base)
    prior = {a.facts.action_id: a for a in old.seo_actions}
    changes = []
    for action in now.seo_actions:
        left, right = _sections(prior[action.facts.action_id]), _sections(action)
        sections = [k for k in right if right[k] != left[k]]
        changes.append(ActionDifference(action_id=action.facts.action_id, evidence_changed=any(k != "decision" for k in sections),
            decision_changed="decision" in sections, changed_sections=sections))
    base.update(actions=changes, new_decisions_changed=[_decision(d) for d in now.seo_new_decisions] != [_decision(d) for d in old.seo_new_decisions])
    return ReviewDifference(state="compared", reason=None, **base)
