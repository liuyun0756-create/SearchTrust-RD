"""Pure entry point for the approved first batch of V22-032 public Findings."""
from itertools import chain

from pydantic import ValidationError

from app.jobs_v22.digest import canonical_json_bytes
from app.report_v22 import site_findings, market_findings, competitor_findings
from app.report_v22.evidence import build_evidence_index
from app.report_v22.evidence_bindings import host
from app.report_v22.evidence_adapters.site_counts import COUNT_VERSION
from app.report_v22.evidence_models import reject_nonfinite
from app.report_v22.findings_common import decision
from app.report_v22.findings_errors import FindingsError
from app.report_v22.findings_models import PublicFindingsInput, PublicFindingsResult, RuleOutcome, RuleTarget
from app.report_v22.findings_rollup import build_rollups
from app.report_v22.findings_view import EvidenceView
from app.report_v22.models import REQUIRED_LAYER_KEYS
from app.report_v22.public_rule_catalog import AHEAD, ASSET, DOMAIN, GBP_RULES, HTTP, NOINDEX, TITLE, RULES, RULE_VERSION


def _validate_count(item, trace, target, *, comparator):
    field = trace.selector.field
    expected_type = target.page_type if field == "eligible_page_type_count" else None
    if (field not in {"eligible_page_type_count", "eligible_html_page_count"}
            or trace.selector.record_context[2:] != [expected_type, COUNT_VERSION]
            or type(item.normalized_value) is not int
            or (item.normalized_value <= 0 if comparator or expected_type is None else item.normalized_value != 0)):
        raise FindingsError("REFERENCE_INVALID")


def _validate_market_context(item, trace, target):
    locator = item.source_locator
    country = {dimension.key: dimension.value for dimension in trace.selector.dimensions}.get("country_code")
    if (locator.query, locator.latitude, locator.longitude, locator.language, locator.device, country) != (
            target.query, target.latitude, target.longitude, target.language, target.device, target.country_code):
        raise FindingsError("REFERENCE_INVALID")
    if trace.selector.field != "status" and trace.selector.record_context[-1] != target.result_type:
        raise FindingsError("REFERENCE_INVALID")


def _validate_outcome(view, outcome):
    evaluation, finding = outcome.evaluation, outcome.finding
    if evaluation.rule_id not in {*RULES, *GBP_RULES} or evaluation.rule_version != RULE_VERSION:
        raise FindingsError("REFERENCE_INVALID")
    if evaluation.rule_id in GBP_RULES and evaluation.state != "not_checked":
        raise FindingsError("REFERENCE_INVALID")
    if (evaluation.state == "triggered") != (finding is not None):
        raise FindingsError("REFERENCE_INVALID")
    if finding is None:
        if evaluation.finding_id is not None:
            raise FindingsError("REFERENCE_INVALID")
    else:
        if (evaluation.finding_id != finding.finding_id or evaluation.rule_id != finding.rule_id
                or evaluation.rule_version != finding.rule_version
                or evaluation.evidence_ids != finding.evidence_ids
                or evaluation.comparator_ids != finding.comparator_ids
                or finding.affected_urls != evaluation.target.urls
                or finding.affected_queries != ([evaluation.target.query] if evaluation.target.query is not None else [])):
            raise FindingsError("REFERENCE_INVALID")
    for key in evaluation.evidence_ids + evaluation.comparator_ids:
        item = view.items.get(key)
        if item is None:
            raise FindingsError("REFERENCE_INVALID")
        if finding and (item.source_type == "coverage" or item.health_status != "healthy"
                        or not view.summaries[item.snapshot_id].business_eligible):
            raise FindingsError("REFERENCE_INVALID")
    if finding is None:
        return
    rule = finding.rule_id
    for key in finding.evidence_ids:
        item = view.items[key]
        expected = "serp" if rule in {DOMAIN, AHEAD} else "site"
        if item.source_type != expected or item.snapshot_id != evaluation.target.snapshot_id:
            raise FindingsError("REFERENCE_INVALID")
        trace = view.traces[key]
        if rule in {HTTP, NOINDEX, TITLE} and item.source_locator.url not in evaluation.target.urls:
            raise FindingsError("REFERENCE_INVALID")
        if rule in {DOMAIN, AHEAD}:
            _validate_market_context(item, trace, evaluation.target)
            if rule == AHEAD and trace.selector.field != "status" and host(item.source_locator.url) != host(view.context.site_url):
                raise FindingsError("REFERENCE_INVALID")
        elif rule == ASSET:
            _validate_count(item, trace, evaluation.target, comparator=False)
    if rule in {HTTP, NOINDEX, TITLE, DOMAIN} and finding.comparator_ids:
        raise FindingsError("REFERENCE_INVALID")
    owners = set()
    known = {host(c.website_url): c.competitor_id for c in view.context.competitors}
    for key in finding.comparator_ids:
        item, trace = view.items[key], view.traces[key]
        if rule == ASSET:
            if item.source_type != "competitor" or trace.selector.competitor_id not in evaluation.target.competitor_ids:
                raise FindingsError("REFERENCE_INVALID")
            _validate_count(item, trace, evaluation.target, comparator=True)
            owners.add(trace.selector.competitor_id)
        elif rule == AHEAD:
            locator, target = item.source_locator, evaluation.target
            owner = known.get(host(locator.url))
            if item.source_type != "serp" or item.snapshot_id != target.snapshot_id or owner not in target.competitor_ids:
                raise FindingsError("REFERENCE_INVALID")
            _validate_market_context(item, trace, target)
            owners.add(owner)
        else:
            raise FindingsError("REFERENCE_INVALID")
    if rule in {ASSET, AHEAD} and (len(owners) < 2 or owners != set(evaluation.target.competitor_ids)):
        raise FindingsError("REFERENCE_INVALID")


def _validate_rollups(view, outcomes, site, clusters):
    findings = {outcome.finding.finding_id: outcome for outcome in outcomes if outcome.finding}
    if site.scope != "sampled_site" or site.page_type is not None or site.finding_ids != sorted(findings):
        raise FindingsError("REFERENCE_INVALID")
    expected_pages = {}
    if view.available("site"):
        for page in view.sources["site"].payload.pages:
            if page.check_status == "checked":
                expected_pages.setdefault(page.page_type, set()).add(page.url)
    if len(clusters) != len(expected_pages) or {cluster.page_type for cluster in clusters} != set(expected_pages):
        raise FindingsError("REFERENCE_INVALID")
    for rollup in [site, *clusters]:
        if (rollup.site_url != view.context.site_url
                or len(set(rollup.evidence_ids)) != len(rollup.evidence_ids)
                or not set(rollup.evidence_ids) <= set(view.items)
                or [layer.layer_key for layer in rollup.layers] != list(REQUIRED_LAYER_KEYS)
                or any(layer.status != "not_checked" or layer.finding_ids or layer.evidence_ids for layer in rollup.layers)):
            raise FindingsError("REFERENCE_INVALID")
    for cluster in clusters:
        urls = expected_pages[cluster.page_type]
        expected_findings = sorted(key for key, outcome in findings.items()
                                   if outcome.evaluation.target.kind in {"page", "title_group"}
                                   and urls.intersection(outcome.finding.affected_urls))
        if (cluster.scope != "sampled_page_type" or cluster.urls != sorted(urls, key=str)
                or cluster.finding_ids != expected_findings):
            raise FindingsError("REFERENCE_INVALID")


def build_public_findings(value: PublicFindingsInput | dict) -> PublicFindingsResult:
    try:
        reject_nonfinite(value)
        raw = value.model_dump(mode="python") if isinstance(value, PublicFindingsInput) else value
        request = PublicFindingsInput.model_validate(raw)
        inputs = request.evidence_input
        if inputs.context.report_type != "prospect" or any(s.kind == "first_party" for s in inputs.sources):
            raise FindingsError("INPUT_INVALID")
        domains = [host(inputs.context.site_url)] + [host(c.website_url) for c in inputs.context.competitors]
        if len(set(domains)) != len(domains):
            raise FindingsError("INPUT_INVALID")
        identifiers = {}
        for source in inputs.sources:
            identifiers.setdefault(source.kind, set()).add(source.binding.snapshot_id)
        if any(len(values) > 1 for values in identifiers.values()):
            raise FindingsError("INPUT_INVALID")
        evidence = build_evidence_index(inputs)
        view = EvidenceView(inputs, evidence)
        gbp = (decision(view, rule, RuleTarget(kind="customer_gbp"), "not_checked", "customer_public_gbp_missing") for rule in GBP_RULES)
        outcomes, findings, encoded_findings = {}, {}, {}
        retained_bytes = len(canonical_json_bytes(evidence))
        if retained_bytes > request.limits.max_bytes:
            raise FindingsError("LIMIT_EXCEEDED")
        for proposed in chain(site_findings.evaluate(view), market_findings.evaluate(view), competitor_findings.evaluate(view), gbp):
            outcome = RuleOutcome.model_validate(proposed.model_dump(mode="python"))
            _validate_outcome(view, outcome)
            if outcome.finding:
                finding = outcome.finding
                encoded = canonical_json_bytes(finding)
                if finding.finding_id in encoded_findings and encoded_findings[finding.finding_id] != encoded:
                    raise FindingsError("ID_CONFLICT")
                if finding.finding_id not in findings:
                    retained_bytes += len(encoded)
                findings[finding.finding_id] = finding
                encoded_findings[finding.finding_id] = encoded
            key = canonical_json_bytes({"rule_id": outcome.evaluation.rule_id, "target": outcome.evaluation.target.model_dump(mode="json")})
            if key in outcomes and canonical_json_bytes(outcomes[key]) != canonical_json_bytes(outcome):
                raise FindingsError("ID_CONFLICT")
            if key not in outcomes:
                retained_bytes += len(canonical_json_bytes(outcome.evaluation))
            outcomes[key] = outcome
            if (len(findings) > request.limits.max_findings or len(outcomes) > request.limits.max_evaluations
                    or retained_bytes > request.limits.max_bytes):
                raise FindingsError("LIMIT_EXCEEDED")
        ordered = [outcomes[key] for key in sorted(outcomes)]
        site, clusters = build_rollups(view, ordered)
        _validate_rollups(view, ordered, site, clusters)
        result = PublicFindingsResult(evidence_result=evidence, findings=[findings[key] for key in sorted(findings)],
            rule_evaluations=[outcome.evaluation for outcome in ordered], site_rollup=site, cluster_rollups=clusters)
        if len(canonical_json_bytes(result)) > request.limits.max_bytes:
            raise FindingsError("LIMIT_EXCEEDED")
        return result
    except (ValidationError, TypeError, ValueError):
        raise FindingsError("INPUT_INVALID") from None
