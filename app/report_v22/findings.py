"""Pure entry point for the approved first batch of V22-032 public Findings."""
from itertools import chain

from pydantic import ValidationError

from app.jobs_v22.digest import canonical_json_bytes
from app.report_v22 import site_findings, market_findings, competitor_findings, public_gbp_findings
from app.report_v22.evidence import build_evidence_index
from app.report_v22.evidence_errors import EvidenceError
from app.report_v22.evidence_bindings import host
from app.report_v22.evidence_adapters.site_counts import COUNT_VERSION
from app.report_v22.evidence_models import EvidenceBuildResult, EvidenceCoverageGap, reject_nonfinite
from app.report_v22.findings_errors import FindingsError
from app.report_v22.findings_models import PublicFindingsInput, PublicFindingsResult, RuleOutcome
from app.report_v22.findings_rollup import build_rollups
from app.report_v22.findings_view import EvidenceView
from app.report_v22.models import REQUIRED_LAYER_KEYS
from app.report_v22.public_rule_catalog import AHEAD, ASSET, DOMAIN, GBP_ALIGNMENT_RULE_VERSION, GBP_RULES, HTTP, NOINDEX, TITLE, RULES, RULE_VERSION
from app.report_v22.site_business_facts import build_site_business_facts
from app.report_v22.site_business_errors import SiteBusinessError
from app.report_v22.site_business_models import SiteBusinessFactsInput
from app.report_v22.site_business_selection import select_site_business_candidates
from app.report_v22.site_gbp_alignment import build_site_gbp_alignment, verify_site_gbp_alignment


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


def _validate_outcome(view, outcome, alignment=None):
    evaluation, finding = outcome.evaluation, outcome.finding
    expected_version = GBP_ALIGNMENT_RULE_VERSION if alignment is not None and evaluation.rule_id in GBP_RULES else RULE_VERSION
    if evaluation.rule_id not in {*RULES, *GBP_RULES} or evaluation.rule_version != expected_version:
        raise FindingsError("REFERENCE_INVALID")
    if evaluation.rule_id in GBP_RULES:
        public_gbp_findings.validate_evaluation(view, evaluation, alignment)
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
        if finding and ((item.source_type == "coverage" and evaluation.rule_id not in GBP_RULES) or item.health_status != "healthy"
                        or not view.summaries[item.snapshot_id].business_eligible):
            raise FindingsError("REFERENCE_INVALID")
    if finding is None:
        return
    rule = finding.rule_id
    if rule in GBP_RULES:
        return
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


def _merge_alignment_evidence(inputs, evidence, extra, maximum):
    items = {item.evidence_id: item for item in evidence.evidence_index}
    traces = {item.evidence_id: item for item in evidence.source_traces}
    sources = {source.binding.snapshot_id: source for source in inputs.sources}
    for item in extra.evidence_index:
        if item.evidence_id in items and items[item.evidence_id] != item:
            raise FindingsError("ID_CONFLICT")
        items[item.evidence_id] = item
    for trace in extra.source_traces:
        if trace.evidence_id in traces and traces[trace.evidence_id] != trace:
            raise FindingsError("ID_CONFLICT")
        traces[trace.evidence_id] = trace
    if set(items) != set(traces) or len(items) > maximum:
        raise FindingsError("LIMIT_EXCEEDED" if len(items) > maximum else "REFERENCE_INVALID")
    counts = {}
    for item in items.values():
        if item.snapshot_id not in sources:
            raise FindingsError("REFERENCE_INVALID")
        counts[item.snapshot_id] = counts.get(item.snapshot_id, 0) + 1
    summaries = []
    for summary in evidence.source_summaries:
        notes = summary.limitations
        if any(item.snapshot_id == summary.snapshot_id for item in extra.evidence_index):
            notes = sorted(set([*notes, "Confirmed-identity site/GBP alignment evidence was added for this offline evaluation."]))
        summaries.append(summary.model_copy(update={"evidence_count": counts.get(summary.snapshot_id, 0), "limitations": notes}))
    gaps = list(evidence.coverage_gaps)
    for item in extra.evidence_index:
        if item.source_type != "coverage":
            continue
        source = sources[item.snapshot_id]
        gaps.append(EvidenceCoverageGap(
            source_type=source.binding.source_type,
            reason="empty",
            snapshot_id=item.snapshot_id,
            evidence_id=item.evidence_id,
            gbp_origin="public_profile" if source.kind == "public_gbp" else None,
        ))
    unique_gaps = {canonical_json_bytes(item): item for item in gaps}
    return EvidenceBuildResult(
        evidence_index=[items[key] for key in sorted(items)],
        source_traces=[traces[key] for key in sorted(traces)],
        source_summaries=summaries,
        coverage_gaps=[unique_gaps[key] for key in sorted(unique_gaps)],
    )


def build_public_findings(value: PublicFindingsInput | dict) -> PublicFindingsResult:
    alignment_enabled = False
    try:
        reject_nonfinite(value)
        raw = value.model_dump(mode="python", warnings=False) if isinstance(value, PublicFindingsInput) else value
        request = PublicFindingsInput.model_validate(raw)
        alignment_enabled = request.business_identity is not None
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
        alignment = None
        if request.business_identity is not None:
            site_source = next((source for source in inputs.sources if source.kind == "site"), None)
            public_source = next((source for source in inputs.sources if source.kind == "public_gbp"), None)
            facts = build_site_business_facts(SiteBusinessFactsInput(
                context={
                    "case_id": inputs.context.case_id,
                    "report_type": inputs.context.report_type,
                    "site_url": inputs.context.site_url,
                    "evaluated_at": inputs.context.evaluated_at,
                },
                source=site_source,
                limits=request.site_business_limits,
            ))
            selection = select_site_business_candidates(facts, request.business_identity, request.site_gbp_alignment_limits)
            extra = build_site_gbp_alignment(
                context=inputs.context,
                identity=request.business_identity,
                facts=facts,
                selection=selection,
                site_source=site_source,
                public_source=public_source,
                evidence=evidence,
                limits=request.site_gbp_alignment_limits,
            )
            verify_site_gbp_alignment(
                extra,
                context=inputs.context,
                identity=request.business_identity,
                facts=facts,
                selection=selection,
                site_source=site_source,
                public_source=public_source,
                evidence=evidence,
                limits=request.site_gbp_alignment_limits,
            )
            alignment = extra.alignment
            evidence = _merge_alignment_evidence(
                inputs,
                evidence,
                extra,
                request.site_gbp_alignment_limits.max_merged_evidence,
            )
        view = EvidenceView(inputs, evidence)
        gbp = (public_gbp_findings.evaluate(view) if alignment is None
               else public_gbp_findings.evaluate(view, alignment))
        outcomes, findings, encoded_findings = {}, {}, {}
        retained_bytes = len(canonical_json_bytes(evidence))
        if retained_bytes > request.limits.max_bytes:
            raise FindingsError("LIMIT_EXCEEDED")
        for proposed in chain(site_findings.evaluate(view), market_findings.evaluate(view), competitor_findings.evaluate(view), gbp):
            outcome = RuleOutcome.model_validate(proposed.model_dump(mode="python"))
            _validate_outcome(view, outcome, alignment)
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
    except EvidenceError as exc:
        if not alignment_enabled:
            raise
        if exc.error_code.endswith("CHECKSUM_MISMATCH"):
            raise FindingsError("CHECKSUM_MISMATCH") from None
        if exc.error_code.endswith("LIMIT_EXCEEDED"):
            raise FindingsError("LIMIT_EXCEEDED") from None
        raise FindingsError("BINDING_INVALID") from None
    except SiteBusinessError as exc:
        if exc.error_code.endswith("CHECKSUM_MISMATCH"):
            raise FindingsError("CHECKSUM_MISMATCH") from None
        if exc.error_code.endswith("LIMIT_EXCEEDED"):
            raise FindingsError("LIMIT_EXCEEDED") from None
        if exc.error_code.endswith("REFERENCE_INVALID") or exc.error_code.endswith("ID_CONFLICT"):
            raise FindingsError("REFERENCE_INVALID") from None
        raise FindingsError("BINDING_INVALID") from None
    except (ValidationError, TypeError, ValueError):
        raise FindingsError("INPUT_INVALID") from None
