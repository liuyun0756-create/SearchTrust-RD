"""Pure deterministic V22-074 verified execution-plan builder."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from pydantic import BaseModel, ValidationError

from app.jobs_v22.digest import canonical_json_bytes, request_digest
from app.report_v22.execution_plan_catalog import (
    METRIC_CATALOG_VERSION,
    PUBLIC_PRESENTATION,
    RULESET_VERSION,
)
from app.report_v22.execution_plan_errors import ExecutionPlanError
from app.report_v22.execution_plan_identity import metric_audit_id
from app.report_v22.execution_plan_models import (
    ExecutableAction,
    ExecutionMetric,
    ExecutionPlanBuildInput,
    ExecutionPlanBuildResult,
    ExecutionRoadmapPhase,
    MetricSelectionAudit,
)
from app.report_v22.models import EvidenceItem, Finding
from app.report_v22.version_diff import build_version_diff
from app.report_v22.verified_reprioritization import build_verified_reprioritization
from app.report_v22.verified_reprioritization_models import QualifiedFindingRef
from app.report_v22.verified_report_assembler import assemble_verified_report


def _validated_input(value: ExecutionPlanBuildInput | dict[str, Any]) -> ExecutionPlanBuildInput:
    try:
        raw = value.model_dump(mode="json", warnings=False) if isinstance(value, BaseModel) else value
        return ExecutionPlanBuildInput.model_validate_json(canonical_json_bytes(raw))
    except (ValidationError, TypeError, ValueError):
        raise ExecutionPlanError("INPUT_INVALID") from None


def _same(left: Any, right: Any) -> bool:
    def value(item: Any) -> Any:
        if isinstance(item, BaseModel):
            return item.model_dump(mode="json")
        if isinstance(item, list):
            return [value(child) for child in item]
        if isinstance(item, tuple):
            return [value(child) for child in item]
        if isinstance(item, dict):
            return {key: value(child) for key, child in item.items()}
        return item
    return canonical_json_bytes(value(left)) == canonical_json_bytes(value(right))


def _checksums(request: ExecutionPlanBuildInput) -> None:
    pairs = (
        (request.verified_reprioritization_input, request.verified_reprioritization_input_checksum),
        (request.verified_reprioritization_result, request.verified_reprioritization_result_checksum),
        (request.version_diff_input, request.version_diff_input_checksum),
        (request.version_diff_result, request.version_diff_result_checksum),
    )
    if any(request_digest(item) != checksum for item, checksum in pairs):
        raise ExecutionPlanError("CHECKSUM_MISMATCH")


def _binding(request: ExecutionPlanBuildInput) -> None:
    verified_input = request.verified_reprioritization_input
    verified_result = request.verified_reprioritization_result
    diff_input = request.version_diff_input
    diff_result = request.version_diff_result
    if (
        verified_input.case_id != request.case_id
        or verified_input.parent_report_id != request.parent_report_id
        or verified_input.evaluated_at != request.evaluated_at
        or verified_input.planning_date != request.planning_date
        or verified_result.case_id != request.case_id
        or verified_result.parent_report_id != request.parent_report_id
        or verified_result.evaluated_at != request.evaluated_at
        or verified_result.planning_date != request.planning_date
        or diff_input.case_id != request.case_id
        or diff_input.parent_report_id != request.parent_report_id
        or diff_input.evaluated_at != request.evaluated_at
        or diff_result.case_id != request.case_id
        or diff_result.parent_report_id != request.parent_report_id
        or diff_result.evaluated_at != request.evaluated_at
        or not _same(diff_input.verified_reprioritization_input, verified_input)
        or not _same(diff_input.verified_reprioritization_result, verified_result)
        or diff_input.verified_reprioritization_input_checksum
        != request.verified_reprioritization_input_checksum
        or diff_input.verified_reprioritization_result_checksum
        != request.verified_reprioritization_result_checksum
        or diff_result.verified_reprioritization_input_checksum
        != request.verified_reprioritization_input_checksum
        or diff_result.verified_reprioritization_result_checksum
        != request.verified_reprioritization_result_checksum
    ):
        raise ExecutionPlanError("BINDING_INVALID")


def _recompute(request: ExecutionPlanBuildInput) -> None:
    try:
        verified = build_verified_reprioritization(request.verified_reprioritization_input)
        diff = build_version_diff(request.version_diff_input)
    except Exception:
        raise ExecutionPlanError("UPSTREAM_MISMATCH") from None
    if not _same(verified, request.verified_reprioritization_result) or not _same(
        diff, request.version_diff_result
    ):
        raise ExecutionPlanError("UPSTREAM_MISMATCH")


def _merge(groups: list[list[Any]], attribute: str, *, equal_duplicates_allowed: bool = False) -> list[Any]:
    values: dict[str, Any] = {}
    for group in groups:
        for item in group:
            key = str(getattr(item, attribute))
            if key in values and not _same(values[key], item):
                raise ExecutionPlanError("ID_CONFLICT")
            if key in values and not equal_duplicates_allowed:
                raise ExecutionPlanError("ID_CONFLICT")
            values[key] = item
    return [values[key] for key in sorted(values)]


def _refs_for_public(action: Any, public_ruleset: str) -> list[QualifiedFindingRef]:
    return [
        QualifiedFindingRef(
            origin_stage="public_findings", ruleset_version=public_ruleset, finding_id=finding_id,
        )
        for finding_id in action.finding_ids
    ]


def _public_metric(action: Any, findings: dict[str, Finding]) -> ExecutionMetric:
    source = action.validation_metrics[0]
    evidence_ids: set[str] = set()
    comparator_ids: set[str] = set()
    for finding_id in action.finding_ids:
        finding = findings[finding_id]
        evidence_ids.update(finding.evidence_ids)
        comparator_ids.update(finding.comparator_ids)
    comparator_ids -= evidence_ids
    return ExecutionMetric(
        metric_key=source.metric_key,
        role="primary",
        baseline_kind="structural_state",
        baseline=source.baseline,
        success_condition=source.success_condition,
        source_types=source.source_types,
        evidence_ids=sorted(evidence_ids),
        comparator_ids=sorted(comparator_ids),
        finding_refs=_refs_for_public(action, "v22_public_findings_v1"),
        evaluator="rule_no_longer_triggers",
        limitations=[
            "This primary metric verifies the saved structural Finding and does not promise a ranking or revenue outcome."
        ],
    )


def _evaluation_indexes(request: ExecutionPlanBuildInput) -> tuple[dict[tuple[str, str], Any], dict[str, Any]]:
    upstream = request.verified_reprioritization_input
    evaluations: dict[tuple[str, str], Any] = {}
    for origin, result in (
        ("first_party_findings", upstream.first_party_result),
        ("cross_source_findings", upstream.cross_source_result),
    ):
        for evaluation in result.rule_evaluations:
            if evaluation.finding_id is not None:
                evaluations[(origin, evaluation.finding_id)] = evaluation
    traces = {
        trace.evidence_id: trace
        for result in (upstream.first_party_result, upstream.cross_source_result)
        for trace in result.evidence_result.source_traces
    }
    return evaluations, traces


def _guardrail(
    *, ranked: Any, relations: dict[str, Any], evaluations: dict[tuple[str, str], Any],
    evidence: dict[str, EvidenceItem], traces: dict[str, Any],
) -> tuple[ExecutionMetric | None, list[str], str]:
    """Select one target-bound verified guardrail, preferring stable sample counts."""
    relation_values = [relations[key] for key in ranked.relation_ids if key in relations]
    for relation in relation_values:
        if relation.relation_kind not in {"supports", "reduces_urgency"}:
            continue
        evaluation = evaluations.get((relation.finding_ref.origin_stage, relation.finding_ref.finding_id))
        if evaluation is None:
            continue
        candidates: list[tuple[int, str, str]] = []
        for evidence_id in evaluation.evidence_ids:
            item = evidence.get(evidence_id)
            trace = traces.get(evidence_id)
            if item is None or trace is None:
                continue
            field = trace.selector.field
            priority = {
                ("gsc", "impressions"): 0,
                ("ga4", "sessions"): 0,
                ("gbp", "change_band"): 0,
                ("gbp", "demand_band"): 1,
                ("gsc", "clicks"): 2,
            }.get((item.source_type, field), 10)
            candidates.append((priority, evidence_id, field))
        if not candidates:
            continue
        _, selected_id, field = sorted(candidates)[0]
        selected = evidence[selected_id]
        context = trace.selector.request_context
        coverage = (
            f"; window={context.start_date}..{context.end_date}"
            if context is not None and context.start_date and context.end_date else ""
        )
        if selected.source_type == "gbp":
            baseline_kind, evaluator = "band", "band_not_worse"
            minimum = "Preserve the same bound public target and comparable observation basis."
            success = f"The verified {field.replace('_', ' ')} band is not worse at the next review."
        else:
            baseline_kind, evaluator = "exact_value", "sample_floor_preserved"
            if selected.source_type == "ga4":
                minimum = "At least 20 sessions on the same bound target and comparable time basis."
            elif field == "clicks":
                minimum = "At least 10 previous-period clicks on the same bound target."
            elif evaluation.target.kind == "page":
                minimum = "At least 100 current-period impressions on the same bound page."
            else:
                minimum = "At least 50 current-period impressions on the same bound query."
            success = f"Preserve the verified {field.replace('_', ' ')} sample floor while evaluating the primary action rule."
        baseline = f"{field}={selected.normalized_value}{coverage}"
        comparators = [
            key for key in evaluation.comparator_ids
            if key != selected_id and key in traces and (
                selected.source_type == "gbp" or traces[key].selector.field == field
            )
        ]
        metric = ExecutionMetric(
            metric_key=f"verified_{selected.source_type}_{field}",
            role="guardrail",
            baseline_kind=baseline_kind,
            baseline=baseline,
            success_condition=success,
            source_types=[selected.source_type],
            evidence_ids=[selected_id],
            comparator_ids=comparators,
            finding_refs=[relation.finding_ref],
            minimum_sample=minimum,
            evaluator=evaluator,
            limitations=[
                "This verified guardrail controls the evaluation sample; it is not an attribution or forecast."
            ],
        )
        return metric, [relation.relation_id], "VERIFIED_TARGET_RELATION"
    return None, [], "NO_TARGET_BOUND_VERIFIED_GUARDRAIL"


def _measurement_metric(ranked: Any) -> ExecutionMetric:
    action = ranked.measurement_action
    if action is None:
        raise ExecutionPlanError("REFERENCE_INVALID")
    consistency = ranked.candidate_kind == "measurement_consistency"
    issue_digest = request_digest(action.issue_codes)
    baseline = (
        f"{len(action.issue_codes)} saved readiness issue codes; "
        f"catalog_digest={issue_digest}"
    )
    return ExecutionMetric(
        metric_key=("verified_comparison_consistency" if consistency else "verified_source_readiness"),
        role="primary",
        baseline_kind="source_readiness",
        baseline=baseline,
        success_condition=(
            "Fresh comparable snapshots no longer trigger the saved direction conflict."
            if consistency
            else "Fresh bound GSC and GA4 snapshots pass health, identity, comparison and required-metric checks."
        ),
        source_types=action.source_types,
        finding_refs=action.finding_refs,
        evaluator=("comparison_conflict_cleared" if consistency else "source_ready"),
        limitations=[
            "No later business action is formally evaluated until this measurement condition passes.",
            "The complete ordered issue-code list remains bound in the verified reprioritization result checksum.",
        ],
    )


def _audit(action: ExecutableAction, relation_ids: list[str], reason: str) -> list[MetricSelectionAudit]:
    metrics = [action.primary_metric] + ([action.guardrail_metric] if action.guardrail_metric else [])
    result = []
    for metric in metrics:
        selection = "measurement_action" if action.candidate_kind != "existing_public" else (
            "verified_relation" if metric.role == "guardrail" else "public_structural_fallback"
        )
        result.append(MetricSelectionAudit(
            audit_id=metric_audit_id(
                action_id=action.action_id, metric_key=metric.metric_key, role=metric.role,
            ),
            action_id=action.action_id,
            metric_key=metric.metric_key,
            role=metric.role,
            selection_basis=selection,
            relation_ids=relation_ids if metric.role == "guardrail" else [],
            evidence_ids=metric.evidence_ids,
            finding_refs=metric.finding_refs,
            reason_code=reason if metric.role == "guardrail" else (
                "MEASUREMENT_ACTION_PRIMARY" if selection == "measurement_action" else "PUBLIC_RULE_PRIMARY"
            ),
        ))
    return result


def _roadmap(action: ExecutableAction, ranked: Any) -> ExecutionRoadmapPhase:
    periods = {1: "days_1_30", 2: "days_31_60", 3: "days_61_90"}
    if ranked.public_action is not None:
        objective = PUBLIC_PRESENTATION[ranked.public_action.template_key].objective
        done = list(ranked.public_action.definition_of_done)
    else:
        from app.report_v22.execution_plan_catalog import MEASUREMENT_PRESENTATION
        objective = MEASUREMENT_PRESENTATION[ranked.measurement_action.template_key].objective
        done = list(ranked.measurement_action.definition_of_done)
    criteria = [*done, action.primary_metric.success_condition]
    if action.guardrail_metric is not None:
        criteria.append(action.guardrail_metric.success_condition)
    if action.execution_gate != "ready":
        criteria.append("The preceding execution gate has passed before this action is evaluated.")
    return ExecutionRoadmapPhase(
        period=periods[action.sequence], action_id=action.action_id,
        execution_gate=action.execution_gate, objective=objective, exit_criteria=criteria,
    )


def validate_execution_plan_privacy(
    request: ExecutionPlanBuildInput, result: ExecutionPlanBuildResult,
) -> None:
    snapshots = request.verified_reprioritization_input.first_party_input.snapshots
    gbp_ids = {str(item.snapshot_id) for item in snapshots if item.source_type == "gbp"}
    payload = result.model_dump(mode="json")
    rendered = canonical_json_bytes(payload)
    if b'"raw_payload"' in rendered or b'"keywords"' in rendered:
        raise ExecutionPlanError("PRIVACY_VIOLATION")
    for item in result.report.evidence_index:
        if str(item.snapshot_id) in gbp_ids and isinstance(item.normalized_value, (int, float)):
            raise ExecutionPlanError("PRIVACY_VIOLATION")


def build_execution_plan(
    value: ExecutionPlanBuildInput | dict[str, Any],
) -> ExecutionPlanBuildResult:
    request = _validated_input(value)
    _checksums(request)
    _binding(request)
    _recompute(request)
    upstream = request.verified_reprioritization_input
    verified = request.verified_reprioritization_result
    parent = request.version_diff_input.parent_report
    parent_before = canonical_json_bytes(parent.model_dump(mode="json"))

    findings = _merge([
        upstream.public_findings_result.findings,
        upstream.first_party_result.findings,
        upstream.cross_source_result.findings,
    ], "finding_id")
    evidence = _merge([
        upstream.public_findings_result.evidence_result.evidence_index,
        upstream.first_party_result.evidence_result.evidence_index,
        upstream.cross_source_result.evidence_result.evidence_index,
    ], "evidence_id", equal_duplicates_allowed=True)
    if (
        len(findings) > request.limits.max_findings
        or len(evidence) > request.limits.max_evidence
        or len(verified.relations) > request.limits.max_relations
    ):
        raise ExecutionPlanError("LIMIT_EXCEEDED")
    finding_by_id = {item.finding_id: item for item in findings}
    evidence_by_id = {item.evidence_id: item for item in evidence}
    relation_by_id = {item.relation_id: item for item in verified.relations}
    evaluations, traces = _evaluation_indexes(request)

    executable: list[ExecutableAction] = []
    audits: list[MetricSelectionAudit] = []
    measurement_gate = verified.actions[0].candidate_kind == "measurement_repair"
    for ranked in verified.actions:
        if ranked.public_action is not None:
            refs = _refs_for_public(ranked.public_action, upstream.public_findings_result.ruleset_version)
            primary = _public_metric(ranked.public_action, finding_by_id)
            guardrail, relation_ids, guardrail_reason = _guardrail(
                ranked=ranked, relations=relation_by_id, evaluations=evaluations,
                evidence=evidence_by_id, traces=traces,
            )
        else:
            refs = list(ranked.measurement_action.finding_refs)
            if not refs:
                refs = [verified.core_problem_finding]
            primary = _measurement_metric(ranked)
            guardrail, relation_ids, guardrail_reason = None, [], "MEASUREMENT_ACTION_PRIMARY"

        if ranked.sequence == 1:
            gate = "blocked_until_measurement_ready" if measurement_gate else "ready"
            blocked: list[str] = []
        elif measurement_gate:
            gate, blocked = "waiting_for_action_1", [verified.actions[0].action_id]
        else:
            gate, blocked = "depends_on_previous", [verified.actions[ranked.sequence - 2].action_id]
        item = ExecutableAction(
            sequence=ranked.sequence,
            action_id=ranked.action_id,
            candidate_kind=ranked.candidate_kind,
            metric_rule_id="V22.EXECUTION." + (
                ranked.public_action.template_key
                if ranked.public_action is not None else ranked.measurement_action.template_key
            ).upper(),
            finding_refs=refs,
            primary_metric=primary,
            guardrail_metric=guardrail,
            review_date=request.planning_date + timedelta(days=30 * ranked.sequence),
            execution_gate=gate,
            blocked_by_action_ids=blocked,
        )
        executable.append(item)
        audits.extend(_audit(item, relation_ids, guardrail_reason))

    roadmap = [_roadmap(item, ranked) for item, ranked in zip(executable, verified.actions, strict=True)]
    limitations = sorted(set([
        "Official GBP Performance is absent unless an authorized GBP snapshot is supplied.",
        "Public GBP evidence verifies structural field alignment only and is not official Performance.",
        "Action success means the corresponding saved rule no longer triggers; it does not guarantee ranking or revenue growth.",
        *(
            value
            for result in (upstream.first_party_result, upstream.cross_source_result)
            for evaluation in result.rule_evaluations
            for value in evaluation.limitations
        ),
        *(
            value
            for item in executable
            for metric in [item.primary_metric, item.guardrail_metric]
            if metric is not None
            for value in metric.limitations
        ),
    ]))
    if (
        any(len(item.finding_refs) > request.limits.max_findings_per_action for item in executable)
        or any(
            len(metric.evidence_ids) > request.limits.max_evidence_per_metric
            for item in executable
            for metric in [item.primary_metric, item.guardrail_metric]
            if metric is not None
        )
        or len(audits) > request.limits.max_audit_entries
        or len(limitations) > request.limits.max_limitations
    ):
        raise ExecutionPlanError("LIMIT_EXCEEDED")
    report = assemble_verified_report(
        parent=parent,
        report_id=request.verified_report_id,
        evaluated_at=request.evaluated_at,
        copy_model_version=request.copy_model_version,
        verified_input=upstream,
        verified_result=verified,
        version_diff_result=request.version_diff_result,
        executable=executable,
        roadmap=roadmap,
        findings=findings,
        evidence=evidence,
        trace_by_id=traces,
        limitations=limitations,
    )
    result = ExecutionPlanBuildResult(
        case_id=request.case_id,
        parent_report_id=request.parent_report_id,
        verified_report_id=request.verified_report_id,
        evaluated_at=request.evaluated_at,
        planning_date=request.planning_date,
        verified_reprioritization_input_checksum=request.verified_reprioritization_input_checksum,
        verified_reprioritization_result_checksum=request.verified_reprioritization_result_checksum,
        version_diff_input_checksum=request.version_diff_input_checksum,
        version_diff_result_checksum=request.version_diff_result_checksum,
        actions=executable,
        roadmap=roadmap,
        metric_audit=audits,
        report=report,
        limitations=limitations,
    )
    if canonical_json_bytes(parent.model_dump(mode="json")) != parent_before:
        raise ExecutionPlanError("UPSTREAM_MISMATCH")
    if len(canonical_json_bytes(result.model_dump(mode="json"))) > request.limits.max_bytes:
        raise ExecutionPlanError("LIMIT_EXCEEDED")
    validate_execution_plan_privacy(request, result)
    return result
