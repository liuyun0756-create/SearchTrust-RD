"""Frozen public snapshots -> specific 2.2.2 findings and execution materials.

No network, model calls, clock reads, payment or persistence. The caller must
freeze the manifest through the authorized repository first. Does not enable a
new intake path or replace the legacy worker before atomic settlement is ready.
"""
from __future__ import annotations

from app.jobs_v22.digest import canonical_json_bytes, request_digest, verified_request_digest
from app.report_v22.action_models import PublicActionPlanInput
from app.report_v22.actions import select_public_action_skeletons
from app.report_v22.capability import AnalysisInputManifest, CapabilityReport, instant
from app.report_v22.findings import build_public_findings
from app.report_v22.findings_models import PublicFindingsInput
from app.report_v22.public_rule_catalog import AHEAD, ASSET, DOMAIN, GBP_RULES, HTTP, NOINDEX, TITLE

LEGACY_RULESET = "v22_capability_public_v1"
PRIORITY_RULESET = "v22_capability_public_v2"
BUSINESS_RULESET = "v22_capability_public_v3"
RULESET = "v22_capability_public_v4"
_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}
_BOUNDARY = "Saved public observations do not establish lost enquiries, a ranking cause or the effect of future work."
_COPY = {
    HTTP: ("Check the saved response before changing the page.", "An unintended HTTP error can prevent a visitor reaching the intended page."),
    NOINDEX: ("Confirm indexing intent before changing the directive.", "An unintended noindex directive can conflict with the intended search availability; actual indexing is not established here."),
    TITLE: ("Review whether each page needs a more distinct, accurate title.", "Distinct titles can clarify which page represents each offer; repeated titles do not prove duplicate body content."),
    DOMAIN: ("Repeat the same search context before drawing a wider visibility conclusion.", "An unobserved domain in this saved sample is a question to investigate, not a ranking cause."),
    AHEAD: ("Review the comparable saved positions, not a site-wide ranking claim.", "The sampled position difference may inform further checks; it does not explain why the difference exists."),
    ASSET: ("Validate the offering and intended page before deciding whether to build anything.", "A sampled page-type difference can prompt a coverage discussion, but does not justify copying a competitor."),
    **{rule: ("Confirm the authoritative business detail before correcting either channel.", "If a contact or location detail is wrong, visitors may have difficulty contacting the intended branch; a valid branch or tracking difference needs no correction.") for rule in GBP_RULES},
}
_MATERIAL = {
    "restore_site_access_indexing": ("technical_check", "Response and indexing check", "instructions",
        "Inspect the recorded response and explicit indexing directive at each target. Confirm the intended policy, correct only an unintended condition and capture a new response. Preserve intentional redirects and exclusions.", []),
    "differentiate_page_titles": ("title_brief", "Page-title editing brief", "requires_confirmation",
        "Prepare a distinct title for each target using its confirmed page purpose. Keep the actual offer accurate; do not add an unverified location, opening hour, service promise or keyword claim. Publish only after the purpose and wording are approved.",
        ["Confirm the distinct purpose and approved offering of each target page."]),
    "close_page_type_gap": ("service_brief", "Service-page decision brief", "requires_confirmation",
        "Check whether an existing page already serves this need. If the offering and need for a distinct page are confirmed, outline its service scope, process, approved proof and contact route. Otherwise record a no-build decision; competitor page counts alone do not justify a new page.",
        ["Confirm the offering and intended page.", "Obtain approved service details and supporting proof before drafting publishable copy."]),
    "align_public_gbp": ("profile_correction", "Public-detail correction brief", "requires_confirmation",
        "Compare the recorded website and public-profile fields for the same business and branch. Confirm the authoritative value and any branch or call-tracking setup. Correct only the confirmed inaccurate channel, then recheck both channels.",
        ["Confirm authoritative business details, branch and tracking configuration."]),
    "review_market_visibility": ("technical_check", "Comparable search recheck", "instructions",
        "Repeat the recorded query, location, language, device and result type. Record the client and confirmed competitor observations together. Use separate page or profile evidence before recommending a causal fix; do not promise a position change.", []),
}


class CapabilityPublicError(ValueError):
    """Safe boundary error; never echo customer text or source payloads."""

    def __init__(self):
        super().__init__("CAPABILITY_PUBLIC_INPUT_OR_RESULT_INVALID")


def _kind(source):
    if source.kind == "public_gbp":
        return "public_gbp"
    return source.binding.source_type


def _bound_public_sources(manifest, value):
    # JSON round-trip revalidates model_copy mutations in nested legacy models.
    request = PublicFindingsInput.model_validate_json(canonical_json_bytes(value))
    context = request.evidence_input.context
    refs = {s.source_kind: s for s in manifest.snapshots if s.source_kind in {"site", "serp", "competitor", "public_gbp"}}
    sources = request.evidence_input.sources
    if (context.case_id != manifest.case_id or context.report_type != "prospect"
            or str(context.site_url) != manifest.business.site_url
            or context.primary_service != manifest.business.primary_service
            or context.target_market.display_name != manifest.business.target_market
            or context.evaluated_at != instant(manifest.evaluated_at)
            or set(_kind(s) for s in sources) != set(refs) or len(sources) != len(refs)
            or sorted(str(c.website_url) for c in context.competitors) != sorted(manifest.business.competitor_sites)):
        raise CapabilityPublicError()
    for source in sources:
        ref, binding = refs[_kind(source)], source.binding
        if (binding.snapshot_id != ref.snapshot_id or binding.case_id != manifest.case_id
                or binding.schema_version != ref.schema_version
                or binding.payload_checksum != ref.payload_checksum
                or request_digest(source.payload) != ref.payload_checksum
                or binding.fetched_at != instant(ref.fetched_at)
                or binding.expires_at != instant(ref.expires_at)
                or binding.health_status != ref.health or binding.identity_match_status != ref.identity_match):
            raise CapabilityPublicError()
    return request


def _target(evaluation, fallback):
    target = evaluation.target
    if target.urls:
        return "\n".join(map(str, target.urls))
    if target.query:
        return f"{target.query} · {target.result_type} · {target.latitude},{target.longitude} · {target.language} · {target.device}"
    if target.page_type:
        return f"{fallback} · sampled {target.page_type} pages"
    return fallback


def _public_report_content(manifest: AnalysisInputManifest, value: PublicFindingsInput) -> dict:
    """Public rule contribution for a fully bound acquisition or service request.

    Callers validate mode, full generation-input checksum and historical binding.
    This contribution must pass final contract validation before any delivery.
    """
    try:
        manifest = AnalysisInputManifest.model_validate_json(canonical_json_bytes(manifest))
        request = _bound_public_sources(manifest, value)
        reviewed = manifest.ruleset_version in {RULESET, "v22_capability_service_v4"}
        result = build_public_findings(request, reviewed_evidence=reviewed)
        skeletons = select_public_action_skeletons(PublicActionPlanInput(
            findings_result=result, planning_date=instant(manifest.evaluated_at).date()),
            all_candidates=manifest.ruleset_version in {RULESET, BUSINESS_RULESET, PRIORITY_RULESET, "v22_capability_service_v2", "v22_capability_service_v3", "v22_capability_service_v4"})
        refs = {s.snapshot_id: s for s in manifest.snapshots}
        original = {e.evidence_id: e for e in result.evidence_result.evidence_index}
        traces = {e.evidence_id: e for e in result.evidence_result.source_traces}
        evaluations = {e.finding_id: e for e in result.rule_evaluations if e.finding_id}
        findings = []
        used = set()
        for finding in sorted(result.findings, key=lambda f: (-_ORDER[f.severity], f.finding_id)):
            ids = sorted(set(finding.evidence_ids + finding.comparator_ids))
            judgment, impact = _COPY[finding.rule_id]
            used.update(ids)
            findings.append(dict(id=finding.finding_id, rule_id=finding.rule_id, rule_version=finding.rule_version,
                classification=finding.classification, severity=finding.severity, confidence=finding.confidence,
                target=_target(evaluations[finding.finding_id], manifest.business.site_url),
                observed_fact=finding.statement, observed_at=max((refs[original[key].snapshot_id].fetched_at for key in ids), key=instant),
                comparison_scope=manifest.business.target_market + " · saved public sample",
                judgment=judgment, interpretation="needs_verification" if finding.rule_id in {ASSET, *GBP_RULES} else "supported",
                impact_path=impact, evidence_ids=ids, limitations=finding.missing_data))
        if not findings:
            # A clean/insufficient sample is not proof that SEO is healthy.
            sample = sorted((e for e in original.values() if e.source_type == "site" and e.health_status == "healthy"), key=lambda e: e.evidence_id)[:3]
            if not sample:
                raise CapabilityPublicError()
            used.update(e.evidence_id for e in sample)
            findings.append(dict(id="fn-public-assessment", rule_id="v22_capability.public_assessment", rule_version="1.0.0",
                classification="fact", severity="low", confidence="low", target=manifest.business.site_url,
                observed_fact="The eligible saved website fields below were checked. The supported public rules did not produce an actionable finding.",
                observed_at=max((refs[e.snapshot_id].fetched_at for e in sample), key=instant),
                comparison_scope="Saved public sample only", judgment="No change is proposed from this evidence. Confirm the scope and unresolved checks before deciding what to investigate next.",
                interpretation="needs_verification", impact_path="No business impact or healthy-site conclusion is established.",
                evidence_ids=[e.evidence_id for e in sample], limitations=[_BOUNDARY]))
        evidence = []
        for key in sorted(used):
            item, trace = original[key], traces[key]
            ref = refs[item.snapshot_id]
            evidence.append(dict(id=key, snapshot_id=item.snapshot_id, source_kind=ref.source_kind,
                object=str(item.source_locator.url or item.source_locator.query or manifest.business.site_url),
                field_path="\n".join(trace.origin_paths), observed_at=ref.fetched_at, value=item.original_value,
                unit=trace.selector.unit, comparison_scope=ref.scope, limitations=item.limitations))
        by_id = {f["id"]: f for f in findings}
        actions = []
        for skeleton in skeletons:
            selected = [by_id[key] for key in skeleton.finding_ids]
            target = "\n".join(dict.fromkeys(f["target"] for f in selected))
            kind, title, readiness, instruction, confirmation = _MATERIAL[skeleton.template_key]
            first = selected[0]
            # Exact rule observation, not model-generated metrics or business facts.
            body = f"Observed: {first['observed_fact']}\n\n{instruction}"
            actions.append(dict(id=skeleton.action_id, title=title, target=target, finding_ids=skeleton.finding_ids,
                steps=[f"Target: {target}", *[step.instruction for step in skeleton.implementation_steps]],
                completion=skeleton.definition_of_done, review=skeleton.validation_metrics[0].success_condition,
                dependencies=skeleton.dependencies, materials=[dict(id=f"{skeleton.action_id}-material", kind=kind,
                    title=title, target=target, body=body, readiness=readiness, confirmation_items=confirmation,
                    evidence_ids=first["evidence_ids"])]))
        lead = by_id[skeletons[0].finding_ids[0]] if skeletons else findings[0]
        summary = (f"{lead['observed_fact']} {lead['judgment']}" if actions else
            f"No action is proposed from the available public evidence for {manifest.business.site_url}. {findings[0]['observed_fact']}")
        limits = [_BOUNDARY]
        if manifest.mode == "acquisition":
            limits.append("Private Search Console, Analytics, lead and revenue data are not included in this public diagnosis.")
        unchecked = sorted({f"{e.rule_id}: {e.reason}" for e in result.rule_evaluations if e.state == "not_checked"})
        if reviewed:
            from app.report_v22.quality_copy import unchecked_limits
            limits.extend(unchecked_limits(result.rule_evaluations))
        else:
            limits.extend(unchecked)
        return dict(schema_version="2.2.2", id=str(manifest.job_id),
            report_type="prospect" if manifest.mode == "acquisition" else "verified_execution",
            version=1 if manifest.previous_report is None else manifest.previous_report.version + 1,
            generated_at=manifest.evaluated_at, input_manifest=manifest.model_dump(mode="json"), title=manifest.business.primary_service,
            summary=summary, evidence=evidence, findings=findings, action_state="actions_supported" if actions else "insufficient_basis",
            actions=actions, limitations=limits, changes=[], settlement_policy="one_credit_per_completed_analysis")
    except (ValueError, TypeError, KeyError, OverflowError):
        raise CapabilityPublicError() from None


def build_public_capability_report(manifest: AnalysisInputManifest | dict, value: PublicFindingsInput | dict) -> CapabilityReport:
    """Pure public generation: specific facts, 0–3 actions, no external calls."""
    try:
        manifest = AnalysisInputManifest.model_validate_json(canonical_json_bytes(manifest))
        request = PublicFindingsInput.model_validate_json(canonical_json_bytes(value))
        if (manifest.mode != "acquisition" or manifest.ruleset_version not in {RULESET, BUSINESS_RULESET, PRIORITY_RULESET, LEGACY_RULESET}
                or verified_request_digest(request.model_dump(mode="json")) != manifest.generation_input_checksum):
            raise CapabilityPublicError()
        raw = _public_report_content(manifest, request)
        if manifest.ruleset_version in {RULESET, BUSINESS_RULESET, PRIORITY_RULESET}:
            from app.report_v22.capability_priority import apply_priority
            apply_priority(raw, manifest)
        report = CapabilityReport.model_validate_json(canonical_json_bytes(raw))
        if len(canonical_json_bytes(report)) > 2_000_000:
            raise CapabilityPublicError()
        return report
    except (ValueError, TypeError, KeyError, OverflowError):
        raise CapabilityPublicError() from None
