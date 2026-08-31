"""Coverage bookkeeping only. The four GBP alignment judgments are not implemented."""
from app.report_v22.evidence_adapters.public_gbp import record_context
from app.report_v22.findings_common import decision
from app.report_v22.findings_errors import FindingsError
from app.report_v22.findings_models import RuleTarget
from app.report_v22.public_rule_catalog import GBP_RULES

RULE_FIELDS = dict(zip(GBP_RULES, ("business_name", "address", "phone", "service_areas")))


def expected_scope(view, rule):
    source = view.sources.get("public_gbp")
    if source is None:
        return RuleTarget(kind="customer_gbp"), "customer_public_gbp_missing", []
    identifier, field = source.binding.snapshot_id, RULE_FIELDS[rule]
    target = RuleTarget(kind="customer_gbp", snapshot_id=identifier)
    if not view.available("public_gbp"):
        reason, category = "source_ineligible", "coverage"
        paths = {"/binding/expires_at", "/binding/health_status", "/binding/identity_match_status"}
    else:
        wrapper = getattr(source.payload.record.fields, field)
        prefix = f"/payload/record/fields/{field}"
        if wrapper.state == "observed":
            reason, category = "gbp_alignment_not_implemented", "public_gbp_field"
            paths = {f"{prefix}/value/{i}" for i in range(len(wrapper.value))} if isinstance(wrapper.value, list) else {f"{prefix}/value"}
        else:
            reason, category, paths = "field_not_observed", "coverage", {f"{prefix}/state"}
    refs = []
    for key, trace in view.traces.items():
        if trace.snapshot_id != identifier or not paths.intersection(trace.origin_paths):
            continue
        selector, item = trace.selector, view.items[key]
        expected_field = trace.origin_paths[0].split("/")[-1] if reason == "source_ineligible" else field
        if (not set(trace.origin_paths) <= paths or selector.category != category
                or selector.field != expected_field or selector.competitor_id is not None
                or selector.request_context is not None
                or selector.record_context != record_context(source, expected_field)
                or item.source_type != ("coverage" if category == "coverage" else "gbp")
                or view.summaries[identifier].gbp_origin != "public_profile"):
            raise FindingsError("REFERENCE_INVALID")
        refs.append(key)
    if not refs:
        raise FindingsError("REFERENCE_INVALID")
    return target, reason, sorted(refs)


def evaluate(view):
    for rule in GBP_RULES:
        target, reason, refs = expected_scope(view, rule)
        yield decision(view, rule, target, "not_checked", reason, evidence=refs)


def validate_evaluation(view, evaluation):
    target, reason, refs = expected_scope(view, evaluation.rule_id)
    if (evaluation.target != target or evaluation.reason != reason or evaluation.evidence_ids != refs
            or evaluation.state != "not_checked" or evaluation.comparator_ids or evaluation.finding_id is not None):
        raise FindingsError("REFERENCE_INVALID")
