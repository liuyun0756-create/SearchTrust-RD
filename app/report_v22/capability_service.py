"""Frozen public + authorized first-party snapshots -> a 2.2.2 service report.

Pure generation only. Caller must use the authorized manifest repository; no
new intake, external requests, payment or historical report is manufactured.
"""
from urllib.parse import urlsplit

from app.jobs_v22.digest import canonical_json_bytes, request_digest, verified_request_digest
from app.jobs_v22.errors import DeterministicJobError
from app.report_v22.capability import AnalysisInputManifest, CapabilityReport, instant
from app.report_v22.capability_public import _public_report_content
from app.report_v22.capability_service_copy import CAUSAL_LIMIT, action_content, interpretation
from app.report_v22.capability_service_inputs import CapabilityServiceInput, ServiceCrossSourceInput
from app.report_v22.cross_source_findings import _evaluate_cross_source_request
from app.report_v22.cross_source_pages import normalize_ga4_page, normalize_gsc_page
from app.report_v22.first_party_checksum import semantic_first_party_input_checksum
from app.report_v22.first_party_findings import _evaluate_first_party_request
from app.report_v22.models import ReportV22

LEGACY_RULESET = "v22_capability_service_v1"
PRIORITY_RULESET = "v22_capability_service_v2"
RULESET = "v22_capability_service_v3"
_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}


class CapabilityServiceError(ValueError):
    def __init__(self): super().__init__("CAPABILITY_SERVICE_INPUT_OR_RESULT_INVALID")


def _validate_history(manifest, previous):
    ref = manifest.previous_report
    if ref is None:
        if previous is not None: raise CapabilityServiceError()
        return
    if not isinstance(previous, dict) or verified_request_digest(previous) != ref.payload_checksum:
        raise CapabilityServiceError()
    if previous.get("schema_version") == "2.2.2":
        prior = CapabilityReport.model_validate_json(canonical_json_bytes(previous))
        identity = (prior.id, prior.input_manifest.case_id, prior.report_type, prior.version)
        if prior.input_manifest.user_id != manifest.user_id: raise CapabilityServiceError()
    else:
        prior = ReportV22.model_validate_json(canonical_json_bytes(previous))
        v = prior.report_version
        identity = (v.report_id, prior.identity.case_id, v.report_type, v.version_number)
    if identity != (ref.report_id, ref.case_id, ref.report_type, ref.version): raise CapabilityServiceError()


def _validated(manifest, value, previous):
    manifest = AnalysisInputManifest.model_validate_json(canonical_json_bytes(manifest))
    request = CapabilityServiceInput.model_validate_json(canonical_json_bytes(value))
    if (manifest.mode not in {"first_service", "after_acquisition"} or manifest.ruleset_version not in {RULESET, PRIORITY_RULESET, LEGACY_RULESET}
            or verified_request_digest(request.model_dump(mode="json")) != manifest.generation_input_checksum):
        raise CapabilityServiceError()
    _validate_history(manifest, previous)
    first = request.first_party_input
    if (first.case_id != manifest.case_id or first.evaluated_at != instant(manifest.evaluated_at)
            or first.parent_report_id != (manifest.previous_report.report_id if manifest.previous_report else None)):
        raise CapabilityServiceError()
    refs = {s.source_kind: s for s in manifest.snapshots if s.binding_id is not None}
    if {"official_gbp" if s.source_type == "gbp" else s.source_type for s in first.snapshots} != set(refs):
        raise CapabilityServiceError()
    for snapshot in first.snapshots:
        ref = refs["official_gbp" if snapshot.source_type == "gbp" else snapshot.source_type]
        if (snapshot.snapshot_id != ref.snapshot_id or snapshot.case_id != manifest.case_id
                or snapshot.binding_id != ref.binding_id or snapshot.schema_version != ref.schema_version
                or snapshot.payload_checksum != ref.payload_checksum or snapshot.fetched_at != instant(ref.fetched_at)
                or snapshot.expires_at != instant(ref.expires_at) or snapshot.health_status != ref.health
                or snapshot.identity_match_status != ref.identity_match):
            raise CapabilityServiceError()
    return manifest, request


def _object(item, fallback):
    locator = item.source_locator
    return str(locator.url or locator.query or locator.page_path or locator.external_resource_id or fallback)


def _scope(item, trace, ref):
    period = next((d.value for d in trace.selector.dimensions if d.key == "period"), "saved")
    dates = f"{item.coverage_start} to {item.coverage_end}" if item.coverage_start and item.coverage_end else "coverage not specified"
    return f"{period} · {dates} · {ref.scope}"


def _select_actions(candidates, findings):
    """Current evidence/rule priority, not P4 business-value ranking.

    Preserve public technical dependencies. Do not drop a dependency to make a
    higher-ranked card fit, and do not fill three slots with trend-only work.
    """
    by_id = {f["id"]: f for f in findings}
    def key(action):
        group = 0 if action["materials"][0]["title"] == "Response and indexing check" else 1 if action["materials"][0]["kind"] == "measurement_check" else 2
        linked = [by_id[fid] for fid in action["finding_ids"]]
        return (group, -max(_RANK[f["severity"]] for f in linked),
                -max(_RANK[f["confidence"]] for f in linked), action["id"])
    selected = []
    pending = list(candidates)
    while pending and len(selected) < 3:
        prior = {a["id"] for a in selected}
        available = [a for a in pending if set(a["dependencies"]) <= prior]
        if not available: break
        chosen = min(available, key=key)
        selected.append(chosen); pending.remove(chosen)
    return selected


def _merge_service_work(candidates, findings):
    """One work package for the same target/check, not one card per metric."""
    by_id={f["id"]:f for f in findings}
    result=[]
    groups={}
    for action in candidates:
        if not action["id"].startswith("ac-service-"):
            result.append(action); continue
        material=action["materials"][0]
        key=(material["kind"],action["target"],action["title"])
        existing=groups.get(key)
        if existing is None:
            groups[key]=action; result.append(action); continue
        existing["finding_ids"]=sorted(set(existing["finding_ids"]+action["finding_ids"]))
        retained=existing["materials"][0]
        retained["evidence_ids"]=sorted(set(retained["evidence_ids"]+material["evidence_ids"]))
        existing["id"]="ac-service-"+request_digest({"findings":existing["finding_ids"],"target":action["target"],"kind":material["kind"]})[7:31]
        retained["id"]=existing["id"]+"-material"
        observed="\n".join(by_id[fid]["observed_fact"] for fid in existing["finding_ids"])
        # Keep the actual observations together; do not silently drop the
        # second metric or generate two identical client work packages.
        instruction=retained["body"].split("\n\n",1)[1]
        retained["body"]=f"Observed:\n{observed}\n\n{instruction}"
    return result


def _retained_source_checks(first_input, refs):
    """Retain concrete checked aggregates even when no intervention triggers.

    Official GBP exact metrics remain excluded per existing retention policy.
    """
    evidence, findings = [], []
    for snapshot in first_input.snapshots:
        if snapshot.source_type not in {"gsc", "ga4"}: continue
        ref = refs[snapshot.snapshot_id]
        current = snapshot.normalized_payload["current"]
        rows = current["totals"]["rows"]
        if not rows: raise CapabilityServiceError()
        fields = ("clicks", "impressions") if snapshot.source_type == "gsc" else ("sessions", "key_events")
        ids = []
        period = f"current · {current['start_date']} to {current['end_date']} · {ref.scope}"
        for field in fields:
            identifier = f"ev-service-checked-{snapshot.source_type}-{field}"
            ids.append(identifier)
            evidence.append(dict(id=identifier, snapshot_id=str(snapshot.snapshot_id), source_kind=snapshot.source_type,
                object=snapshot.external_resource_id, field_path=f"/current/totals/rows/0/{field}",
                observed_at=ref.fetched_at, value=rows[0][field], unit="count", comparison_scope=period,
                limitations=[CAUSAL_LIMIT]))
        findings.append(dict(id=f"fn-service-checked-{snapshot.source_type}", rule_id="v22_capability.service_source_check", rule_version="1.0.0",
            classification="fact", severity="low", confidence="high", target=snapshot.external_resource_id,
            observed_fact=f"The {snapshot.source_type.upper()} current-period totals below were checked for the bound resource.",
            observed_at=ref.fetched_at, comparison_scope=period, judgment="These totals establish the checked scope, not that SEO is healthy or that an edit is needed.",
            interpretation="supported", impact_path="No business cause, qualified-lead count or revenue effect is established by these totals.",
            evidence_ids=ids, limitations=[CAUSAL_LIMIT]))
    return evidence, findings


def build_service_capability_report(manifest, value, *, previous_report=None) -> CapabilityReport:
    try:
        manifest, request = _validated(manifest, value, previous_report)
        raw = _public_report_content(manifest, request.public_input)
        first_input = request.first_party_input
        first = _evaluate_first_party_request(first_input)
        cross_input = ServiceCrossSourceInput.model_validate_json(canonical_json_bytes(dict(
            case_id=str(manifest.case_id), parent_report_id=str(first_input.parent_report_id) if first_input.parent_report_id else None,
            evaluated_at=manifest.evaluated_at, normalized_domain=urlsplit(manifest.business.site_url).hostname,
            first_party_input=first_input.model_dump(mode="json"), first_party_result=first.model_dump(mode="json"),
            first_party_input_checksum=semantic_first_party_input_checksum(first_input), first_party_result_checksum=request_digest(first))))
        cross = _evaluate_cross_source_request(cross_input, first_party_builder=_evaluate_first_party_request)
        refs = {s.snapshot_id: s for s in manifest.snapshots}
        evidence = {e["id"]: e for e in raw["evidence"]}
        checked_evidence, checked_findings = _retained_source_checks(first_input, refs)
        evidence.update({e["id"]: e for e in checked_evidence})
        raw["findings"].extend(checked_findings)
        candidates = list(raw["actions"])
        assessments = []
        for result in (first, cross):
            originals = {e.evidence_id: e for e in result.evidence_result.evidence_index}
            traces = {e.evidence_id: e for e in result.evidence_result.source_traces}
            evaluations = {e.finding_id: e for e in result.rule_evaluations if e.finding_id}
            assessments.extend(f"{e.rule_id}: {e.reason}" for e in result.rule_evaluations if e.state == "not_checked")
            for finding in result.findings:
                evaluation = evaluations[finding.finding_id]
                ids = sorted(set(finding.evidence_ids + finding.comparator_ids))
                judgment, impact, state = interpretation(finding, evaluation)
                for key in ids:
                    item, trace = originals[key], traces[key]
                    ref = refs[item.snapshot_id]
                    item_copy = dict(id=key, snapshot_id=str(item.snapshot_id), source_kind=ref.source_kind,
                        object=_object(item, manifest.business.site_url), field_path="\n".join(trace.origin_paths),
                        observed_at=ref.fetched_at, value=item.original_value, unit=trace.selector.unit,
                        comparison_scope=_scope(item, trace, ref), limitations=item.limitations)
                    if key in evidence and evidence[key] != item_copy: raise CapabilityServiceError()
                    evidence[key] = item_copy
                target = evaluation.target.key
                source_type = getattr(evaluation.target,"source_type",None)
                if evaluation.target.kind == "coverage" and source_type:
                    source = next(s for s in first_input.snapshots if s.source_type == source_type)
                    target = f"{source.external_resource_id} · {target}"
                scopes = list(dict.fromkeys(evidence[key]["comparison_scope"] for key in ids))
                f = dict(id=finding.finding_id, rule_id=finding.rule_id, rule_version=finding.rule_version,
                    classification=finding.classification, severity=finding.severity, confidence=finding.confidence,
                    target=target, observed_fact=finding.statement,
                    observed_at=max((evidence[key]["observed_at"] for key in ids), key=instant),
                    comparison_scope="\n".join(scopes), judgment=judgment, interpretation=state,
                    impact_path=impact, evidence_ids=ids, limitations=finding.missing_data)
                raw["findings"].append(f)
                content = action_content(finding, evaluation)
                if content is None: continue
                work_target=target
                if evaluation.target.kind in {"page","landing_page"}:
                    domain=urlsplit(manifest.business.site_url).hostname
                    canonical=normalize_gsc_page(evaluation.target.key,domain) or normalize_ga4_page(evaluation.target.key,domain)
                    if canonical: work_target=canonical
                    elif evaluation.finding_kind=="business": continue
                identifier = "ac-service-" + request_digest({"finding": finding.finding_id, "kind": content["kind"]})[7:31]
                candidate = dict(id=identifier, title=content["title"], target=work_target, finding_ids=[finding.finding_id],
                    steps=[f"Target: {work_target}", content["instruction"]], completion=content["completion"], review=content["review"], dependencies=[],
                    materials=[dict(id=f"{identifier}-material", kind=content["kind"], title=content["title"], target=work_target,
                        body=f"Observed: {finding.statement}\n\nScope: {f['comparison_scope']}\n\n{content['instruction']}",
                        readiness=content["readiness"], confirmation_items=content["confirmations"], evidence_ids=ids)])
                candidates.append(candidate)
        raw["evidence"] = sorted(evidence.values(), key=lambda e: e["id"])
        candidates = _merge_service_work(candidates, raw["findings"])
        if manifest.ruleset_version in {RULESET, PRIORITY_RULESET}:
            from app.report_v22.capability_priority import apply_priority
            raw["actions"] = candidates
            apply_priority(raw, manifest)
        else:
            raw["actions"] = _select_actions(candidates, raw["findings"])
        raw["action_state"] = "actions_supported" if raw["actions"] else "insufficient_basis"
        lead = next((f for f in raw["findings"] if raw["actions"] and f["id"] == raw["actions"][0]["finding_ids"][0]), raw["findings"][0])
        raw["summary"] = f"{lead['observed_fact']} {lead['judgment']}" if raw["actions"] else "No action is proposed from the eligible public and connected evidence. Review the recorded observations and unresolved checks."
        raw["limitations"] = list(dict.fromkeys([*raw["limitations"], CAUSAL_LIMIT, *sorted(set(assessments))]))
        # Pair/source availability is not silently replaced by a zero value.
        if not any(s.source_kind == "official_gbp" for s in manifest.snapshots):
            raw["limitations"].append("Official GBP Performance was not connected; public business-profile observations are a separate source.")
        report = CapabilityReport.model_validate_json(canonical_json_bytes(raw))
        if len(canonical_json_bytes(report)) > 2_000_000: raise CapabilityServiceError()
        return report
    except (ValueError, TypeError, KeyError, OverflowError, DeterministicJobError):
        raise CapabilityServiceError() from None
