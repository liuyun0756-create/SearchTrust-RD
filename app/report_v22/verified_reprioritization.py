"""Pure deterministic V22-072 verified action reprioritization."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from urllib.parse import urlsplit

from pydantic import ValidationError

from app.jobs_v22.digest import canonical_json_bytes, request_digest
from app.report_v22.action_errors import PublicActionError
from app.report_v22.action_models import PublicActionPlanInput
from app.report_v22.actions import (
    _add_dependencies,
    _build_action,
    _build_candidates,
    _candidate_sort_key,
    _validated_references,
    canonical_public_findings,
    build_public_action_plan,
)
from app.report_v22.cross_source_findings import build_cross_source_findings
from app.report_v22.cross_source_findings_errors import CrossSourceFindingsError
from app.report_v22.cross_source_findings_models import CrossSourceFindingsInput, PAIR_SOURCES
from app.report_v22.evidence_models import reject_nonfinite
from app.report_v22.findings import build_public_findings
from app.report_v22.findings_errors import FindingsError
from app.report_v22.first_party_findings import build_first_party_findings
from app.report_v22.first_party_findings_errors import FirstPartyFindingsError
from app.report_v22.verified_reprioritization_errors import VerifiedReprioritizationError
from app.report_v22.verified_reprioritization_identity import measurement_action_id, relation_id
from app.report_v22.verified_reprioritization_mapping import (
    CROSS_MEASUREMENT, FIRST_AUDIT_PREFIXES, build_business_relations,
)
from app.report_v22.verified_reprioritization_models import (
    FindingRelation,
    MeasurementAction,
    QualifiedFindingRef,
    RankedVerifiedAction,
    UnusedFinding,
    VerifiedReprioritizationInput,
    VerifiedReprioritizationResult,
    VerifiedSelectionAudit,
)
from app.report_v22.models import ImplementationStep


RULESET_VERSION = "v22_verified_reprioritization_v1"


@dataclass
class _RankedCandidate:
    candidate_key: str
    action_id: str
    kind: str
    verification_level: str
    verification_rank: int
    relation_ids: list[str]
    public: Any | None = None
    measurement: MeasurementAction | None = None
    base_sort: tuple[Any, ...] = ()


def _validated_input(value: VerifiedReprioritizationInput | dict[str, Any]) -> VerifiedReprioritizationInput:
    try:
        reject_nonfinite(value)
        raw = value.model_dump(mode="json", warnings=False) if isinstance(value, VerifiedReprioritizationInput) else value
        return VerifiedReprioritizationInput.model_validate_json(canonical_json_bytes(raw))
    except (ValidationError, TypeError, ValueError):
        raise VerifiedReprioritizationError("INPUT_INVALID") from None


def _same(left: Any, right: Any) -> bool:
    return canonical_json_bytes(left) == canonical_json_bytes(right)


def _validate_checksums(request: VerifiedReprioritizationInput) -> None:
    pairs = (
        (request.public_findings_input, request.public_findings_input_checksum),
        (canonical_public_findings(request.public_findings_result), request.public_findings_result_checksum),
        (request.public_action_plan, request.public_action_plan_checksum),
        (request.first_party_input, request.first_party_input_checksum),
        (request.first_party_result, request.first_party_result_checksum),
        (request.cross_source_result, request.cross_source_result_checksum),
    )
    if any(request_digest(value) != checksum for value, checksum in pairs):
        raise VerifiedReprioritizationError("CHECKSUM_MISMATCH")


def _validate_binding(request: VerifiedReprioritizationInput) -> None:
    context = request.public_findings_input.evidence_input.context
    try:
        public_host = (urlsplit(str(context.site_url)).hostname or "").lower().removeprefix("www.")
    except ValueError:
        raise VerifiedReprioritizationError("BINDING_INVALID") from None
    domain = request.cross_source_normalized_domain.lower().removeprefix("www.")
    if (
        context.case_id != request.case_id
        or context.report_type != "prospect"
        or context.evaluated_at > request.evaluated_at
        or request.first_party_input.case_id != request.case_id
        or request.first_party_input.parent_report_id != request.parent_report_id
        or request.first_party_input.evaluated_at != request.evaluated_at
        or public_host != domain
    ):
        raise VerifiedReprioritizationError("BINDING_INVALID")


def _recompute(request: VerifiedReprioritizationInput) -> None:
    try:
        public = canonical_public_findings(build_public_findings(request.public_findings_input))
        if not _same(public, canonical_public_findings(request.public_findings_result)):
            raise VerifiedReprioritizationError("UPSTREAM_MISMATCH")
        action_plan = build_public_action_plan(PublicActionPlanInput(
            findings_result=public,
            planning_date=request.public_action_plan.planning_date,
            limits=request.public_action_limits,
        ))
        if not _same(action_plan, request.public_action_plan):
            raise VerifiedReprioritizationError("UPSTREAM_MISMATCH")
        first_party = build_first_party_findings(request.first_party_input)
        if not _same(first_party, request.first_party_result):
            raise VerifiedReprioritizationError("UPSTREAM_MISMATCH")
        cross = build_cross_source_findings(CrossSourceFindingsInput(
            case_id=request.case_id,
            parent_report_id=request.parent_report_id,
            evaluated_at=request.evaluated_at,
            normalized_domain=request.cross_source_normalized_domain,
            first_party_input=request.first_party_input,
            first_party_result=first_party,
            first_party_input_checksum=request.first_party_input_checksum,
            first_party_result_checksum=request.first_party_result_checksum,
            limits=request.cross_source_limits,
        ))
        if not _same(cross, request.cross_source_result):
            raise VerifiedReprioritizationError("UPSTREAM_MISMATCH")
    except VerifiedReprioritizationError:
        raise
    except (FindingsError, PublicActionError, FirstPartyFindingsError, CrossSourceFindingsError, ValidationError):
        raise VerifiedReprioritizationError("UPSTREAM_MISMATCH") from None


def _candidates(request: VerifiedReprioritizationInput) -> list[Any]:
    result = canonical_public_findings(request.public_findings_result)
    try:
        findings, evaluations, evidence_sources = _validated_references(
            result, max_findings=request.public_action_limits.max_findings,
        )
        candidates = _build_candidates(
            result, findings, evaluations, evidence_sources,
            max_candidates=min(request.public_action_limits.max_candidates, request.limits.max_public_candidates),
            max_findings_per_action=request.public_action_limits.max_findings_per_action,
            max_targets_per_action=request.public_action_limits.max_targets_per_action,
        )
    except PublicActionError as exc:
        kind = "LIMIT_EXCEEDED" if exc.error_code.endswith("LIMIT_EXCEEDED") else "REFERENCE_INVALID"
        raise VerifiedReprioritizationError(kind) from None
    audits = {item.candidate_key: item for item in request.public_action_plan.selection_audit}
    if len(audits) != len(request.public_action_plan.selection_audit) or set(audits) != {item.candidate_key for item in candidates}:
        raise VerifiedReprioritizationError("REFERENCE_INVALID")
    for candidate in candidates:
        audit = audits[candidate.candidate_key]
        if candidate.anchor is None or candidate.priority is None or (
            audit.action_id != candidate.action_id
            or audit.template_key != candidate.template.key
            or audit.anchor_finding_id != candidate.anchor.finding_id
            or audit.finding_ids != sorted(item.finding_id for item, _ in candidate.entries)
            or audit.priority != candidate.priority
        ):
            raise VerifiedReprioritizationError("REFERENCE_INVALID")
    return candidates


def _blocking_issues(request: VerifiedReprioritizationInput) -> tuple[list[str], list[str], list[QualifiedFindingRef]]:
    issues: set[str] = set()
    sources: set[str] = set()
    refs: list[QualifiedFindingRef] = []
    for assessment in request.first_party_result.source_assessments:
        if assessment.source_type not in {"gsc", "ga4"} or assessment.state == "eligible_for_business":
            continue
        sources.add(assessment.source_type)
        issues.add(f"{assessment.source_type.upper()}_{assessment.health_status.upper()}")
        issues.update(f"{assessment.source_type.upper()}_{reason}" for reason in assessment.reasons)
    for evaluation in request.first_party_result.rule_evaluations:
        if evaluation.finding_id is None or evaluation.target.source_type not in {"gsc", "ga4"}:
            continue
        if any(evaluation.rule_id == prefix or evaluation.rule_id.startswith(f"{prefix}.") for prefix in FIRST_AUDIT_PREFIXES):
            sources.add(evaluation.target.source_type)
            issues.add(evaluation.rule_id)
            refs.append(QualifiedFindingRef(
                origin_stage="first_party_findings",
                ruleset_version=request.first_party_result.ruleset_version,
                finding_id=evaluation.finding_id,
            ))
    for evaluation in request.cross_source_result.rule_evaluations:
        if (
            evaluation.target.pair == "gsc_ga4"
            and evaluation.finding_kind == "business"
            and evaluation.target.kind in {"aggregate", "weekly_series"}
            and evaluation.reason in {"provider_limitation", "comparison_unavailable"}
        ):
            sources.update(PAIR_SOURCES[evaluation.target.pair])
            issues.add(f"GSC_GA4_{evaluation.reason.upper()}")
    if len(issues) > request.limits.max_issue_codes:
        raise VerifiedReprioritizationError("LIMIT_EXCEEDED")
    return sorted(sources, key=("gsc", "ga4", "gbp").index), sorted(issues), sorted(
        refs, key=lambda item: (item.origin_stage, item.ruleset_version, item.finding_id),
    )


def _steps(template_key: str) -> list[ImplementationStep]:
    values = (
        (
            ("Confirm source access", "Confirm the affected Google authorization and selected resource."),
            ("Repair collection", "Resolve the saved health, identity, comparison, or provider limitation."),
            ("Resynchronize", "Collect fresh GSC and GA4 snapshots for the same Case and comparable windows."),
            ("Verify readiness", "Re-run health and comparison checks before generating verified actions."),
        )
        if template_key == "restore_verified_measurement"
        else (
            ("Confirm the comparison", "Recheck both sources with the same identity and time-window basis."),
            ("Inspect instrumentation", "Review collection and attribution boundaries without assuming either source is wrong."),
            ("Repeat the measurement", "Collect fresh comparable snapshots and re-evaluate the direction conflict."),
        )
    )
    return [ImplementationStep(sequence=index, title=title, instruction=instruction) for index, (title, instruction) in enumerate(values, 1)]


def _measurement_action(
    *, template_key: str, source_types: list[str], issue_codes: list[str],
    finding_refs: list[QualifiedFindingRef], review_date: Any,
) -> MeasurementAction:
    action_id = measurement_action_id(
        template_key=template_key, source_types=source_types, issue_codes=issue_codes,
    )
    done = (
        ["Fresh bound GSC and GA4 inputs pass the required health, identity, and comparison checks."]
        if template_key == "restore_verified_measurement"
        else ["Fresh comparable source snapshots no longer trigger the grouped direction conflict, or the limitation is documented."]
    )
    return MeasurementAction(
        action_id=action_id,
        template_key=template_key,
        source_types=source_types,
        issue_codes=issue_codes,
        finding_refs=finding_refs,
        implementation_steps=_steps(template_key),
        definition_of_done=done,
        review_date=review_date,
    )


def _ranked_business(candidates: list[Any], relations: list[FindingRelation]) -> list[_RankedCandidate]:
    by_candidate: dict[str, list[FindingRelation]] = {}
    for item in relations:
        if item.candidate_key is not None:
            by_candidate.setdefault(item.candidate_key, []).append(item)
    ranked: list[_RankedCandidate] = []
    for candidate in candidates:
        related = by_candidate.get(candidate.candidate_key, [])
        support_origins = {item.finding_ref.origin_stage for item in related if item.relation_kind == "supports"}
        growth = any(item.relation_kind == "reduces_urgency" for item in related)
        if "cross_source_findings" in support_origins:
            rank, level = 6, "cross_source_supported"
        elif "first_party_findings" in support_origins:
            rank, level = 4, "single_source_supported"
        else:
            rank, level = 2, "public_only"
        if growth:
            rank, level = max(0, rank - 2), "reduced_by_growth"
        ranked.append(_RankedCandidate(
            candidate_key=candidate.candidate_key,
            action_id=candidate.action_id,
            kind="existing_public",
            verification_level=level,
            verification_rank=rank,
            relation_ids=sorted(item.relation_id for item in related),
            public=candidate,
            base_sort=_candidate_sort_key(candidate),
        ))
    return sorted(ranked, key=_rank_key)


def _rank_key(item: _RankedCandidate) -> tuple[Any, ...]:
    return (-item.verification_rank, *item.base_sort, item.candidate_key, item.action_id)


def _conflict_candidates(request: VerifiedReprioritizationInput) -> tuple[list[_RankedCandidate], list[FindingRelation]]:
    groups: dict[str, list[Any]] = {}
    for evaluation in request.cross_source_result.rule_evaluations:
        if evaluation.finding_id is not None and evaluation.rule_id in CROSS_MEASUREMENT:
            groups.setdefault(evaluation.target.pair, []).append(evaluation)
        elif evaluation.finding_id is not None and evaluation.finding_kind == "measurement":
            raise VerifiedReprioritizationError("UNSUPPORTED_RULE")
    candidates: list[_RankedCandidate] = []
    relations: list[FindingRelation] = []
    for pair in sorted(groups):
        evaluations = sorted(groups[pair], key=lambda item: (item.rule_id, item.target.kind, item.target.key, item.finding_id))
        refs = [QualifiedFindingRef(
            origin_stage="cross_source_findings",
            ruleset_version=request.cross_source_result.ruleset_version,
            finding_id=item.finding_id,
        ) for item in evaluations]
        issue_codes = sorted({item.rule_id for item in evaluations})
        action = _measurement_action(
            template_key="review_measurement_consistency",
            source_types=list(PAIR_SOURCES[pair]),
            issue_codes=issue_codes,
            finding_refs=refs,
            review_date=request.planning_date + timedelta(days=30),
        )
        candidate_key = f"measurement-consistency-{pair}"
        for evaluation, ref in zip(evaluations, refs):
            relations.append(FindingRelation(
                relation_id=relation_id(
                    finding_ref=ref, relation_kind="measurement_conflict", candidate_key=candidate_key,
                ),
                finding_ref=ref,
                relation_kind="measurement_conflict",
                candidate_key=candidate_key,
                action_id=action.action_id,
                target_key=evaluation.target.key,
                reason_code="CROSS_SOURCE_DIRECTION_CONFLICT",
            ))
        candidates.append(_RankedCandidate(
            candidate_key=candidate_key,
            action_id=action.action_id,
            kind="measurement_consistency",
            verification_level="measurement_conflict",
            verification_rank=3,
            relation_ids=sorted(item.relation_id for item in relations if item.action_id == action.action_id),
            measurement=action,
            base_sort=(-2, -2, -3, -min(len(refs), 5), pair),
        ))
    return sorted(candidates, key=_rank_key), sorted(relations, key=lambda item: item.relation_id)


def _order_selected(selected: list[_RankedCandidate], *, site_url: Any) -> list[_RankedCandidate]:
    public = [item.public for item in selected if item.public is not None]
    _add_dependencies(public, site_url=site_url)
    by_id = {item.action_id: item for item in selected}
    completed: set[str] = set()
    ordered: list[_RankedCandidate] = []
    while len(ordered) < len(selected):
        available = []
        for item in selected:
            if item.action_id in completed:
                continue
            dependencies = set(item.public.dependencies if item.public is not None else [])
            if not dependencies <= set(by_id):
                raise VerifiedReprioritizationError("DEPENDENCY_INVALID")
            if dependencies <= completed:
                available.append(item)
        if not available:
            raise VerifiedReprioritizationError("DEPENDENCY_INVALID")
        current = sorted(available, key=_rank_key)[0]
        ordered.append(current)
        completed.add(current.action_id)
    return ordered


def build_verified_reprioritization(
    value: VerifiedReprioritizationInput | dict[str, Any],
) -> VerifiedReprioritizationResult:
    request = _validated_input(value)
    try:
        request.planning_date + timedelta(days=90)
    except OverflowError:
        raise VerifiedReprioritizationError("INPUT_INVALID") from None
    if len(canonical_json_bytes(request)) > request.limits.max_bytes:
        raise VerifiedReprioritizationError("LIMIT_EXCEEDED")
    _validate_binding(request)
    _validate_checksums(request)
    _recompute(request)
    candidates = _candidates(request)
    new_count = len(request.first_party_result.findings) + len(request.cross_source_result.findings)
    if new_count > request.limits.max_new_findings:
        raise VerifiedReprioritizationError("LIMIT_EXCEEDED")
    try:
        relations = build_business_relations(
            candidates=candidates,
            first_party_result=request.first_party_result,
            cross_source_result=request.cross_source_result,
            normalized_domain=request.cross_source_normalized_domain,
        )
    except KeyError:
        raise VerifiedReprioritizationError("UNSUPPORTED_RULE") from None
    except ValueError:
        raise VerifiedReprioritizationError("ID_CONFLICT") from None
    conflicts, conflict_relations = _conflict_candidates(request)
    relations.extend(conflict_relations)
    relations.sort(key=lambda item: item.relation_id)

    source_types, issue_codes, blocker_refs = _blocking_issues(request)
    blocker: _RankedCandidate | None = None
    if issue_codes:
        action = _measurement_action(
            template_key="restore_verified_measurement",
            source_types=source_types,
            issue_codes=issue_codes,
            finding_refs=blocker_refs,
            review_date=request.planning_date + timedelta(days=30),
        )
        candidate_key = "measurement-repair-required"
        blocker_relation_ids: list[str] = []
        blocker_ref_set = {(item.origin_stage, item.ruleset_version, item.finding_id) for item in blocker_refs}
        relations = [item for item in relations if (
            item.finding_ref.origin_stage, item.finding_ref.ruleset_version, item.finding_ref.finding_id
        ) not in blocker_ref_set]
        for ref in blocker_refs:
            relation = FindingRelation(
                relation_id=relation_id(finding_ref=ref, relation_kind="supports", candidate_key=candidate_key),
                finding_ref=ref,
                relation_kind="supports",
                candidate_key=candidate_key,
                action_id=action.action_id,
                target_key="verified_measurement",
                reason_code="BLOCKING_MEASUREMENT_ISSUE",
            )
            relations.append(relation)
            blocker_relation_ids.append(relation.relation_id)
        blocker = _RankedCandidate(
            candidate_key=candidate_key,
            action_id=action.action_id,
            kind="measurement_repair",
            verification_level="forced_measurement_repair",
            verification_rank=100,
            relation_ids=sorted(blocker_relation_ids),
            measurement=action,
            base_sort=(),
        )
    relations.sort(key=lambda item: item.relation_id)
    if len(relations) > request.limits.max_relations:
        raise VerifiedReprioritizationError("LIMIT_EXCEEDED")

    business = _ranked_business(candidates, relations)
    fallback_id: str | None = None
    if blocker is not None:
        if len(business) < 2:
            raise VerifiedReprioritizationError("INSUFFICIENT_ACTIONS")
        selected = [blocker, *business[:2]]
        ordered = [blocker, *_order_selected(business[:2], site_url=request.public_findings_result.site_rollup.site_url)]
        all_ranked = [blocker, *business, *conflicts]
    else:
        all_normal = sorted([*business, *conflicts], key=_rank_key)
        if len(all_normal) < 3:
            raise VerifiedReprioritizationError("INSUFFICIENT_ACTIONS")
        selected = all_normal[:3]
        if not any(item.kind == "existing_public" for item in selected):
            selected[-1] = business[0]
            fallback_id = business[0].action_id
        ordered = _order_selected(selected, site_url=request.public_findings_result.site_rollup.site_url)
        all_ranked = all_normal

    highest_business = sorted(
        [item for item in selected if item.public is not None], key=_rank_key,
    )[0]
    core = QualifiedFindingRef(
        origin_stage="public_findings",
        ruleset_version=request.public_findings_result.ruleset_version,
        finding_id=highest_business.public.anchor.finding_id,
    )
    selected_ids = {item.action_id for item in selected}
    ranked_actions: list[RankedVerifiedAction] = []
    for sequence, item in enumerate(ordered, 1):
        review_date = request.planning_date + timedelta(days=30 * sequence)
        public_action = None
        measurement_action = None
        if item.public is not None:
            try:
                public_action = _build_action(
                    item.public, sequence=sequence, review_date=review_date,
                    limits=request.public_action_limits,
                )
            except PublicActionError:
                raise VerifiedReprioritizationError("REFERENCE_INVALID") from None
        else:
            measurement_action = item.measurement.model_copy(update={"review_date": review_date})
        ranked_actions.append(RankedVerifiedAction(
            sequence=sequence,
            action_id=item.action_id,
            candidate_kind=item.kind,
            verification_level=item.verification_level,
            verification_rank=item.verification_rank,
            relation_ids=item.relation_ids,
            public_action=public_action,
            measurement_action=measurement_action,
        ))

    audits: list[VerifiedSelectionAudit] = []
    for item in sorted(all_ranked, key=lambda candidate: candidate.candidate_key):
        is_selected = item.action_id in selected_ids
        if item is blocker:
            reason = "forced_measurement_repair"
        elif is_selected:
            reason = (
                "selected_business_fallback"
                if item.action_id == fallback_id
                else "selected_verified_top_three"
            )
        elif blocker is not None and item.kind == "measurement_consistency":
            reason = "excluded_when_measurement_blocked"
        else:
            reason = "lower_verified_priority"
        anchor = None
        priority = None
        if item.public is not None:
            anchor = QualifiedFindingRef(
                origin_stage="public_findings",
                ruleset_version=request.public_findings_result.ruleset_version,
                finding_id=item.public.anchor.finding_id,
            )
            priority = item.public.priority
        audits.append(VerifiedSelectionAudit(
            candidate_key=item.candidate_key,
            action_id=item.action_id,
            candidate_kind=item.kind,
            public_anchor=anchor,
            base_priority=priority,
            verification_level=item.verification_level,
            verification_rank=item.verification_rank,
            relation_ids=item.relation_ids,
            selection_state="selected" if is_selected else "unselected",
            reason=reason,
        ))
    if len(audits) > request.limits.max_audit_entries:
        raise VerifiedReprioritizationError("LIMIT_EXCEEDED")

    relation_by_ref: dict[tuple[str, str, str], list[FindingRelation]] = {}
    for item in relations:
        key = (item.finding_ref.origin_stage, item.finding_ref.ruleset_version, item.finding_ref.finding_id)
        relation_by_ref.setdefault(key, []).append(item)
    unused: list[UnusedFinding] = []
    for origin, result in (
        ("first_party_findings", request.first_party_result),
        ("cross_source_findings", request.cross_source_result),
    ):
        for finding in result.findings:
            ref = QualifiedFindingRef(
                origin_stage=origin,
                ruleset_version=result.ruleset_version,
                finding_id=finding.finding_id,
            )
            related = relation_by_ref.get((origin, result.ruleset_version, finding.finding_id), [])
            if any(item.action_id in selected_ids for item in related):
                continue
            reason = "not_selected"
            if related and all(item.reason_code == "AUDIT_ONLY_RULE" for item in related):
                reason = "audit_only_rule"
            elif related and all(item.relation_kind == "unmatched" for item in related):
                reason = "no_strict_target_match"
            unused.append(UnusedFinding(finding_ref=ref, reason=reason))
    unused.sort(key=lambda item: (
        item.finding_ref.origin_stage, item.finding_ref.ruleset_version, item.finding_ref.finding_id,
    ))

    try:
        result = VerifiedReprioritizationResult(
            case_id=request.case_id,
            parent_report_id=request.parent_report_id,
            evaluated_at=request.evaluated_at,
            planning_date=request.planning_date,
            public_findings_result_checksum=request.public_findings_result_checksum,
            public_action_plan_checksum=request.public_action_plan_checksum,
            first_party_result_checksum=request.first_party_result_checksum,
            cross_source_result_checksum=request.cross_source_result_checksum,
            actions=ranked_actions,
            core_problem_finding=core,
            relations=relations,
            selection_audit=audits,
            unused_findings=unused,
        )
    except (ValidationError, ValueError):
        raise VerifiedReprioritizationError("REFERENCE_INVALID") from None
    if len(canonical_json_bytes(result)) > request.limits.max_bytes:
        raise VerifiedReprioritizationError("LIMIT_EXCEEDED")
    return result
