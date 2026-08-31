"""Shared outcome construction: scope, deterministic prose and confidence bounds."""
from app.report_v22.evidence_identity import stable_key
from app.report_v22.findings_errors import FindingsError
from app.report_v22.findings_identity import finding_id
from app.report_v22.findings_models import RuleEvaluation, RuleOutcome
from app.report_v22.models import Finding
from app.report_v22.public_rule_catalog import RULES, RULE_VERSION

SAMPLE_NOTE = "These observations concern the saved sample only, not complete site or market coverage."


def decision(view, rule_id, target, state, reason, *, evidence=(), comparators=(),
             statement=None, notes=(), severity=None):
    evidence = sorted(set(evidence))
    comparators = sorted(set(comparators))
    limitations = sorted(set([SAMPLE_NOTE, *notes, *view.notes(evidence + comparators)]))
    finding = None
    if state == "triggered":
        if not evidence or not statement or rule_id not in RULES:
            raise FindingsError("REFERENCE_INVALID")
        spec = RULES[rule_id]
        order = {"low": 0, "medium": 1, "high": 2}
        confidence = min([spec.confidence] + [view.items[key].confidence for key in evidence + comparators], key=order.get)
        identifier = finding_id(case_id=view.context.case_id, rule_id=rule_id, rule_version=RULE_VERSION,
                                target=target, evidence_ids=evidence, comparator_ids=comparators)
        finding = Finding(
            finding_id=identifier, statement=statement, evidence_ids=evidence,
            comparator_ids=comparators, rule_id=rule_id, rule_version=RULE_VERSION,
            classification=spec.classification, severity=severity or spec.severity,
            scope=f"{target.kind}:{stable_key(target)[:24]}", confidence=confidence,
            affected_urls=target.urls, affected_queries=[target.query] if target.query is not None else [],
            missing_data=limitations, change_conditions=[spec.change_condition],
        )
    return RuleOutcome(evaluation=RuleEvaluation(
        rule_id=rule_id, rule_version=RULE_VERSION, target=target, state=state, reason=reason,
        evidence_ids=evidence, comparator_ids=comparators,
        finding_id=finding.finding_id if finding else None, limitations=limitations,
    ), finding=finding)
