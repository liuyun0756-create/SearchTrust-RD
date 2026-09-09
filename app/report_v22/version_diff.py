"""Pure deterministic V22-073 changed-only version difference builder."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from pydantic import BaseModel, ValidationError

from app.jobs_v22.digest import canonical_json_bytes, request_digest
from app.report_v22.models import PreviousFindingReference, VersionDiff, VersionDiffEntry
from app.report_v22.verified_reprioritization import (
    _candidates,
    build_verified_reprioritization,
)
from app.report_v22.verified_reprioritization_mapping import normalize_query
from app.report_v22.cross_source_pages import normalize_ga4_page, normalize_gsc_page
from app.report_v22.version_diff_errors import VersionDiffError
from app.report_v22.version_diff_identity import diff_audit_id, finding_fingerprint
from app.report_v22.version_diff_models import (
    VersionDiffAuditEntry,
    VersionDiffBuildInput,
    VersionDiffBuildResult,
)
from app.report_v22.verified_reprioritization_models import QualifiedFindingRef


RULESET_VERSION = "v22_version_diff_v1"
REASON_CATALOG_VERSION = "v22_version_diff_reasons_v1"

REASONS = {
    "REFINED_BY_RELIABLE_GROWTH": (
        "Reliable growth evidence lowers the urgency of this finding without refuting the saved public observation."
    ),
    "REPRIORITIZED_BY_VERIFIED_ACTION_ORDER": (
        "Verified evidence changed whether the related action is selected or where it appears in the top three."
    ),
    "REPRIORITIZED_BY_MEASUREMENT_GATE": (
        "Source health, identity, or comparison readiness changed the action order; the cited public evidence still supports the original business finding and is not a refutation."
    ),
    "CONFIRMED_BY_VERIFIED_EVIDENCE": (
        "New verified evidence supports this saved public finding while its related action position remains unchanged."
    ),
    "NEW_VERIFIED_FINDING": (
        "This verified finding was not part of the saved public report and does not duplicate evidence already used to explain an older finding."
    ),
}


def _validated_input(value: VersionDiffBuildInput | dict[str, Any]) -> VersionDiffBuildInput:
    try:
        raw = value.model_dump(mode="json", warnings=False) if isinstance(value, VersionDiffBuildInput) else value
        return VersionDiffBuildInput.model_validate_json(canonical_json_bytes(raw))
    except (ValidationError, TypeError, ValueError):
        raise VersionDiffError("INPUT_INVALID") from None


def _same(left: Any, right: Any) -> bool:
    def jsonable(value: Any) -> Any:
        if isinstance(value, BaseModel):
            return value.model_dump(mode="json")
        if isinstance(value, list):
            return [jsonable(item) for item in value]
        if isinstance(value, tuple):
            return [jsonable(item) for item in value]
        if isinstance(value, dict):
            return {key: jsonable(item) for key, item in value.items()}
        return value

    return canonical_json_bytes(jsonable(left)) == canonical_json_bytes(jsonable(right))


def _validate_checksums(request: VersionDiffBuildInput) -> None:
    pairs = (
        (request.parent_report, request.parent_report_checksum),
        (request.verified_reprioritization_input, request.verified_reprioritization_input_checksum),
        (request.verified_reprioritization_result, request.verified_reprioritization_result_checksum),
    )
    if any(request_digest(value) != checksum for value, checksum in pairs):
        raise VersionDiffError("CHECKSUM_MISMATCH")


def _validate_binding(request: VersionDiffBuildInput) -> None:
    parent = request.parent_report
    upstream = request.verified_reprioritization_input
    result = request.verified_reprioritization_result
    if (
        parent.report_version.schema_version != "2.2.0"
        or parent.report_version.report_type != "prospect"
        or parent.identity.case_id != request.case_id
        or parent.report_version.report_id != request.parent_report_id
        or parent.report_version.parent_report_id is not None
        or parent.version_diff.kind != "initial"
        or parent.version_diff.parent_report_id is not None
        or parent.version_diff.entries
        or upstream.case_id != request.case_id
        or upstream.parent_report_id != request.parent_report_id
        or upstream.evaluated_at != request.evaluated_at
        or result.case_id != request.case_id
        or result.parent_report_id != request.parent_report_id
        or result.evaluated_at != request.evaluated_at
    ):
        raise VersionDiffError("BINDING_INVALID")


def _target_text(target: Any) -> str:
    if target.kind in {"url", "site"}:
        return str(target.url)
    if target.kind == "query":
        return target.query or ""
    if target.kind == "page_type":
        return target.page_type or ""
    return f"Google Business Profile field: {target.gbp_field}"


def _validate_parent(request: VersionDiffBuildInput) -> None:
    parent = request.parent_report
    upstream = request.verified_reprioritization_input
    if not _same(parent.findings, upstream.public_findings_result.findings):
        raise VersionDiffError("PARENT_MISMATCH")
    if not _same(parent.evidence_index, upstream.public_findings_result.evidence_result.evidence_index):
        raise VersionDiffError("PARENT_MISMATCH")
    expected = {item.action_id: item for item in upstream.public_action_plan.actions}
    if set(expected) != {item.action_id for item in parent.top_actions}:
        raise VersionDiffError("PARENT_MISMATCH")
    for actual in parent.top_actions:
        source = expected[actual.action_id]
        expected_metrics = [
            {
                "metric_key": item.metric_key,
                "baseline": item.baseline,
                "success_condition": item.success_condition,
                "source_type": item.source_types[0],
            }
            for item in source.validation_metrics
        ]
        if (
            actual.sequence != source.sequence
            or actual.finding_ids != source.finding_ids
            or actual.exact_targets != [_target_text(item) for item in source.exact_targets]
            or actual.implementation_steps != source.implementation_steps
            or actual.specification != source.specification
            or actual.required_client_assets != source.required_client_assets
            or actual.dependencies != source.dependencies
            or actual.owner_suggestion != source.owner_suggestion
            or actual.effort_bucket != source.effort_bucket
            or actual.definition_of_done != source.definition_of_done
            or [item.model_dump(mode="json") for item in actual.validation_metrics] != expected_metrics
            or actual.data_sources != source.data_sources
            or actual.review_date != source.review_date
        ):
            raise VersionDiffError("PARENT_MISMATCH")


def _recompute(request: VersionDiffBuildInput) -> None:
    try:
        rebuilt = build_verified_reprioritization(request.verified_reprioritization_input)
    except Exception:
        raise VersionDiffError("UPSTREAM_MISMATCH") from None
    if not _same(rebuilt, request.verified_reprioritization_result):
        raise VersionDiffError("UPSTREAM_MISMATCH")


def _merge_by_id(groups: list[list[Any]], attribute: str) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for group in groups:
        for item in group:
            key = getattr(item, attribute)
            previous = merged.get(key)
            if previous is not None and not _same(previous, item):
                raise VersionDiffError("ID_CONFLICT")
            merged[key] = item
    return merged


def _new_indexes(request: VersionDiffBuildInput) -> tuple[dict[tuple[str, str, str], Any], dict[str, Any]]:
    upstream = request.verified_reprioritization_input
    qualified: dict[tuple[str, str, str], Any] = {}
    for origin, result in (
        ("first_party_findings", upstream.first_party_result),
        ("cross_source_findings", upstream.cross_source_result),
    ):
        for finding in result.findings:
            key = (origin, result.ruleset_version, finding.finding_id)
            if key in qualified and not _same(qualified[key], finding):
                raise VersionDiffError("ID_CONFLICT")
            qualified[key] = finding
    evidence = _merge_by_id(
        [
            upstream.public_findings_result.evidence_result.evidence_index,
            upstream.first_party_result.evidence_result.evidence_index,
            upstream.cross_source_result.evidence_result.evidence_index,
        ],
        "evidence_id",
    )
    all_findings = _merge_by_id(
        [
            upstream.public_findings_result.findings,
            upstream.first_party_result.findings,
            upstream.cross_source_result.findings,
        ],
        "finding_id",
    )
    if len(all_findings) != (
        len(upstream.public_findings_result.findings)
        + len(upstream.first_party_result.findings)
        + len(upstream.cross_source_result.findings)
    ):
        # Equal cross-stage IDs are forbidden even when their content happens to match.
        raise VersionDiffError("ID_CONFLICT")
    return qualified, evidence


def _evaluation_kind(request: VersionDiffBuildInput) -> dict[tuple[str, str, str], str]:
    upstream = request.verified_reprioritization_input
    values: dict[tuple[str, str, str], str] = {}
    for origin, result in (
        ("first_party_findings", upstream.first_party_result),
        ("cross_source_findings", upstream.cross_source_result),
    ):
        for item in result.rule_evaluations:
            if item.finding_id is not None:
                values[(origin, result.ruleset_version, item.finding_id)] = item.target.kind
    return values


def _owned_finding_ids(candidate: Any, relation: Any, kind: str | None, domain: str) -> set[str]:
    if relation.relation_kind not in {"supports", "reduces_urgency"}:
        return set()
    owned: set[str] = set()
    for target in candidate.targets:
        if kind in {"page", "landing_page"} and target.kind == "url" and target.url is not None:
            raw = relation.target_key.rsplit("|", 1)[0] if kind == "landing_page" else relation.target_key
            left = normalize_ga4_page(raw, domain) if kind == "landing_page" else normalize_gsc_page(raw, domain)
            right = normalize_gsc_page(str(target.url), domain)
            if left is not None and left == right:
                owned.update(target.finding_ids)
        elif kind == "query" and target.kind == "query" and target.query is not None:
            if normalize_query(relation.target_key) == normalize_query(target.query):
                owned.update(target.finding_ids)
        elif kind in {"aggregate", "search_demand"} and target.kind == "site":
            owned.update(target.finding_ids)
    return owned


def _finding_evidence(finding: Any, known: dict[str, Any]) -> list[str]:
    ids = sorted(set(finding.evidence_ids + finding.comparator_ids))
    if not ids or any(item not in known for item in ids):
        raise VersionDiffError("REFERENCE_INVALID")
    return ids


def _previous(parent_id: Any, finding: Any) -> PreviousFindingReference:
    return PreviousFindingReference(
        report_id=parent_id,
        finding_id=finding.finding_id,
        statement=finding.statement,
        fingerprint=finding_fingerprint(finding),
    )


def _position_maps(request: VersionDiffBuildInput) -> tuple[dict[str, int | None], dict[str, int | None]]:
    upstream = request.verified_reprioritization_input
    result = request.verified_reprioritization_result
    old_selected = {item.action_id: item.sequence for item in upstream.public_action_plan.actions}
    new_selected = {item.action_id: item.sequence for item in result.actions}
    old = {item.action_id: old_selected.get(item.action_id) for item in upstream.public_action_plan.selection_audit}
    current = {item.action_id: new_selected.get(item.action_id) for item in result.selection_audit}
    return old, current


def _decision_relations(action_id: str, request: VersionDiffBuildInput) -> list[Any]:
    result = request.verified_reprioritization_result
    direct = [item for item in result.relations if item.action_id == action_id and item.relation_kind != "unmatched"]
    if direct:
        return direct
    old, current = _position_maps(request)
    changed = {
        key for key in set(old) | set(current)
        if old.get(key) != current.get(key)
    }
    return [item for item in result.relations if item.action_id in changed and item.relation_kind != "unmatched"]


def build_version_diff(value: VersionDiffBuildInput | dict[str, Any]) -> VersionDiffBuildResult:
    request = _validated_input(value)
    if len(canonical_json_bytes(request)) > request.limits.max_bytes:
        raise VersionDiffError("LIMIT_EXCEEDED")
    _validate_binding(request)
    _validate_checksums(request)
    _validate_parent(request)
    _recompute(request)

    upstream = request.verified_reprioritization_input
    verified = request.verified_reprioritization_result
    if (
        len(request.parent_report.findings) > request.limits.max_parent_findings
        or len(upstream.first_party_result.findings) + len(upstream.cross_source_result.findings) > request.limits.max_new_findings
        or len(verified.relations) > request.limits.max_relations
    ):
        raise VersionDiffError("LIMIT_EXCEEDED")
    new_by_ref, evidence_by_id = _new_indexes(request)
    if len(evidence_by_id) > request.limits.max_current_evidence:
        raise VersionDiffError("LIMIT_EXCEEDED")

    candidates = _candidates(upstream)
    candidate_by_key = {item.candidate_key: item for item in candidates}
    kind_by_ref = _evaluation_kind(request)
    old_by_id = {item.finding_id: item for item in request.parent_report.findings}
    relations_by_old: dict[str, list[Any]] = defaultdict(list)
    for relation in verified.relations:
        candidate = candidate_by_key.get(relation.candidate_key or "")
        ref_key = (
            relation.finding_ref.origin_stage,
            relation.finding_ref.ruleset_version,
            relation.finding_ref.finding_id,
        )
        if candidate is None:
            continue
        for old_id in _owned_finding_ids(
            candidate,
            relation,
            kind_by_ref.get(ref_key),
            upstream.cross_source_normalized_domain,
        ):
            if old_id not in old_by_id:
                raise VersionDiffError("REFERENCE_INVALID")
            relations_by_old[old_id].append(relation)

    candidate_by_finding: dict[str, list[Any]] = defaultdict(list)
    for candidate in candidates:
        for finding, _ in candidate.entries:
            candidate_by_finding[finding.finding_id].append(candidate)
    old_position, new_position = _position_maps(request)

    records: list[tuple[tuple[Any, ...], VersionDiffEntry, VersionDiffAuditEntry]] = []
    unchanged: list[PreviousFindingReference] = []
    consumed: set[tuple[str, str, str]] = set()
    for finding in request.parent_report.findings:
        direct = sorted(relations_by_old.get(finding.finding_id, []), key=lambda item: item.relation_id)
        candidates_for_finding = sorted(candidate_by_finding.get(finding.finding_id, []), key=lambda item: item.candidate_key)
        changed_candidate = next((
            item for item in candidates_for_finding
            if old_position.get(item.action_id) != new_position.get(item.action_id)
        ), None)
        if any(item.relation_kind == "reduces_urgency" for item in direct):
            change_type, reason_code = "refined", "REFINED_BY_RELIABLE_GROWTH"
            selected_relations = [item for item in direct if item.relation_kind == "reduces_urgency"]
            candidate = changed_candidate or next(
                (item for item in candidates_for_finding if item.action_id == selected_relations[0].action_id), None,
            )
        elif changed_candidate is not None:
            change_type, reason_code = "reprioritized", "REPRIORITIZED_BY_VERIFIED_ACTION_ORDER"
            selected_relations = direct or _decision_relations(changed_candidate.action_id, request)
            candidate = changed_candidate
        elif any(item.relation_kind == "supports" for item in direct):
            change_type, reason_code = "confirmed", "CONFIRMED_BY_VERIFIED_EVIDENCE"
            selected_relations = [item for item in direct if item.relation_kind == "supports"]
            candidate = next(
                (item for item in candidates_for_finding if item.action_id == selected_relations[0].action_id), None,
            )
        else:
            unchanged.append(_previous(request.parent_report_id, finding))
            continue

        refs: dict[tuple[str, str, str], QualifiedFindingRef] = {}
        for relation in selected_relations:
            key = (
                relation.finding_ref.origin_stage,
                relation.finding_ref.ruleset_version,
                relation.finding_ref.finding_id,
            )
            if key in new_by_ref:
                refs[key] = relation.finding_ref
        evidence_ids = sorted({
            evidence_id
            for key in refs
            for evidence_id in _finding_evidence(new_by_ref[key], evidence_by_id)
        })
        evidence_basis = "direct_relation" if direct and evidence_ids else "ranking_boundary"
        issue_codes: list[str] = []
        measurement_actions = [
            action.measurement_action
            for action in verified.actions
            if action.measurement_action is not None
            and action.action_id in {item.action_id for item in selected_relations}
        ]
        if measurement_actions:
            reason_code = "REPRIORITIZED_BY_MEASUREMENT_GATE"
            issue_codes = sorted({
                code for action in measurement_actions for code in action.issue_codes
            })
        if not evidence_ids:
            evidence_ids = _finding_evidence(finding, evidence_by_id)
            evidence_basis = "parent_fallback"
            reason_code = "REPRIORITIZED_BY_MEASUREMENT_GATE"
            issue_codes = sorted({
                code
                for action in verified.actions
                if action.measurement_action is not None
                and action.candidate_kind == "measurement_repair"
                for code in action.measurement_action.issue_codes
            })
        current_refs = [
            QualifiedFindingRef(
                origin_stage="public_findings",
                ruleset_version=upstream.public_findings_result.ruleset_version,
                finding_id=finding.finding_id,
            ),
            *refs.values(),
        ]
        current_ids = sorted({item.finding_id for item in current_refs})
        if (
            len(current_ids) > request.limits.max_current_findings_per_entry
            or len(evidence_ids) > request.limits.max_evidence_per_entry
        ):
            raise VersionDiffError("LIMIT_EXCEEDED")
        previous = _previous(request.parent_report_id, finding)
        entry = VersionDiffEntry(
            change_type=change_type,
            previous_finding=previous,
            current_finding_ids=current_ids,
            evidence_ids=evidence_ids,
            reason=REASONS[reason_code],
        )
        audit = VersionDiffAuditEntry(
            audit_id=diff_audit_id(
                parent_report_id=request.parent_report_id,
                change_type=change_type,
                previous_finding_id=finding.finding_id,
                current_finding_refs=current_refs,
            ),
            change_type=change_type,
            previous_finding_id=finding.finding_id,
            current_finding_refs=current_refs,
            relation_ids=[item.relation_id for item in selected_relations],
            action_id=candidate.action_id if candidate is not None else None,
            reason_code=reason_code,
            decision_issue_codes=issue_codes,
            evidence_ids=evidence_ids,
            evidence_basis=evidence_basis,
        )
        consumed.update(refs)
        new_sequence = new_position.get(candidate.action_id) if candidate is not None else None
        old_sequence = old_position.get(candidate.action_id) if candidate is not None else None
        order = (0, new_sequence, finding.finding_id) if new_sequence is not None else (
            1, old_sequence if old_sequence is not None else 99, candidate.candidate_key if candidate else "", finding.finding_id,
        )
        records.append((order, entry, audit))

    new_refs: list[QualifiedFindingRef] = []
    for key in sorted(new_by_ref):
        if key in consumed:
            continue
        finding = new_by_ref[key]
        ref = QualifiedFindingRef(origin_stage=key[0], ruleset_version=key[1], finding_id=key[2])
        evidence_ids = _finding_evidence(finding, evidence_by_id)
        entry = VersionDiffEntry(
            change_type="new",
            previous_finding=None,
            current_finding_ids=[finding.finding_id],
            evidence_ids=evidence_ids,
            reason=REASONS["NEW_VERIFIED_FINDING"],
        )
        audit = VersionDiffAuditEntry(
            audit_id=diff_audit_id(
                parent_report_id=request.parent_report_id,
                change_type="new",
                previous_finding_id=None,
                current_finding_refs=[ref],
            ),
            change_type="new",
            previous_finding_id=None,
            current_finding_refs=[ref],
            relation_ids=[
                item.relation_id for item in verified.relations if item.finding_ref == ref
            ],
            action_id=None,
            reason_code="NEW_VERIFIED_FINDING",
            decision_issue_codes=[],
            evidence_ids=evidence_ids,
            evidence_basis="new_finding",
        )
        records.append(((2, key[0], finding.rule_id, key[2]), entry, audit))
        new_refs.append(ref)

    records.sort(key=lambda item: item[0])
    entries = [item[1] for item in records]
    audit = [item[2] for item in records]
    if len(entries) > request.limits.max_entries or len(audit) > request.limits.max_audit_entries:
        raise VersionDiffError("LIMIT_EXCEEDED")
    try:
        result = VersionDiffBuildResult(
            case_id=request.case_id,
            parent_report_id=request.parent_report_id,
            evaluated_at=request.evaluated_at,
            parent_report_checksum=request.parent_report_checksum,
            verified_reprioritization_input_checksum=request.verified_reprioritization_input_checksum,
            verified_reprioritization_result_checksum=request.verified_reprioritization_result_checksum,
            version_diff=VersionDiff(
                kind="upgrade",
                parent_report_id=request.parent_report_id,
                entries=entries,
            ),
            audit=audit,
            unchanged_previous_findings=sorted(unchanged, key=lambda item: item.finding_id),
            consumed_new_findings=[
                QualifiedFindingRef(origin_stage=key[0], ruleset_version=key[1], finding_id=key[2])
                for key in sorted(consumed)
            ],
            new_findings=sorted(
                new_refs,
                key=lambda item: (item.origin_stage, item.ruleset_version, item.finding_id),
            ),
        )
    except (ValidationError, ValueError):
        raise VersionDiffError("REFERENCE_INVALID") from None
    if len(canonical_json_bytes(result)) > request.limits.max_bytes:
        raise VersionDiffError("LIMIT_EXCEEDED")
    return result
