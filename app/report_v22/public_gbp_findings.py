"""Legacy coverage bookkeeping and opt-in audited site/GBP judgments."""
from app.report_v22.evidence_adapters.public_gbp import record_context
from app.report_v22.findings_common import decision
from app.report_v22.findings_errors import FindingsError
from app.report_v22.findings_models import RuleTarget
from app.report_v22.public_rule_catalog import GBP_ALIGNMENT_RULE_VERSION, GBP_RULES

RULE_FIELDS = dict(zip(GBP_RULES, ("business_name", "address", "phone", "service_areas")))
ALIGNMENT_FIELDS = dict(zip(GBP_RULES, ("business_name", "address", "phone", "service_area")))
STATEMENTS = {
    "business_name": "The eligible website business name does not align with the saved public GBP business name.",
    "address": "The eligible website address does not align with the saved public GBP address.",
    "phone": "The eligible website phone does not align with the saved public GBP phone.",
    "service_area": "The eligible website service areas do not fully align with the saved public GBP service areas.",
}
REASONS = {
    "exact_match": "exact_match",
    "semantic_match": "semantic_match",
    "partial_match": "partial_match",
    "mismatch": "value_mismatch",
    "site_missing": "site_field_missing",
    "gbp_missing": "gbp_field_missing",
    "both_missing": "both_fields_missing",
    "not_applicable": "field_not_applicable",
}


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


def _alignment_outcome(view, alignment, rule):
    field = next((item for item in alignment.fields if item.field == ALIGNMENT_FIELDS[rule]), None)
    if field is None:
        raise FindingsError("REFERENCE_INVALID")
    target = RuleTarget(kind="customer_gbp", snapshot_id=alignment.site_snapshot_id, urls=field.urls)
    items = view.items
    site_coverage = [key for key in field.coverage_evidence_ids if items.get(key) and items[key].original_value == "site_missing"]
    gbp_coverage = [key for key in field.coverage_evidence_ids if items.get(key) and items[key].original_value == "gbp_missing"]
    evidence = sorted(set([*field.site_evidence_ids, *site_coverage]))
    comparators = sorted(set([*field.gbp_evidence_ids, *gbp_coverage]))
    if len(site_coverage) + len(gbp_coverage) != len(field.coverage_evidence_ids):
        raise FindingsError("REFERENCE_INVALID")
    if field.state == "not_checked":
        return decision(view, rule, target, "not_checked", field.not_checked_reason,
                        evidence=evidence, comparators=comparators,
                        notes=field.limitations, rule_version=GBP_ALIGNMENT_RULE_VERSION)
    state = "triggered" if field.state in {"partial_match", "mismatch", "site_missing", "gbp_missing", "both_missing"} else "not_triggered"
    return decision(
        view,
        rule,
        target,
        state,
        REASONS[field.state],
        evidence=evidence,
        comparators=comparators,
        statement=STATEMENTS[field.field] if state == "triggered" else None,
        notes=field.limitations,
        severity="low" if field.state == "partial_match" else None,
        rule_version=GBP_ALIGNMENT_RULE_VERSION,
    )


def evaluate(view, alignment=None):
    if alignment is not None:
        for rule in GBP_RULES:
            yield _alignment_outcome(view, alignment, rule)
        return
    for rule in GBP_RULES:
        target, reason, refs = expected_scope(view, rule)
        yield decision(view, rule, target, "not_checked", reason, evidence=refs)


def validate_evaluation(view, evaluation, alignment=None):
    if alignment is not None:
        expected = _alignment_outcome(view, alignment, evaluation.rule_id).evaluation
        if evaluation != expected:
            raise FindingsError("REFERENCE_INVALID")
        return
    target, reason, refs = expected_scope(view, evaluation.rule_id)
    if (evaluation.target != target or evaluation.reason != reason or evaluation.evidence_ids != refs
            or evaluation.state != "not_checked" or evaluation.comparator_ids or evaluation.finding_id is not None):
        raise FindingsError("REFERENCE_INVALID")
