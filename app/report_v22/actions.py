"""Pure deterministic selection of exactly three v2.2 public action skeletons."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from pydantic import ValidationError

from app.jobs_v22.digest import canonical_json_bytes, request_digest
from app.report_v22.action_errors import PublicActionError
from app.report_v22.action_models import (
    ActionCopyRequirements,
    ActionPriority,
    ActionSelectionAudit,
    ActionSkeleton,
    ActionTarget,
    ActionValidationMetric,
    PublicActionPlan,
    PublicActionPlanInput,
)
from app.report_v22.evidence_bindings import host
from app.report_v22.evidence_models import reject_nonfinite
from app.report_v22.findings_models import PublicFindingsResult, RuleEvaluation
from app.report_v22.models import ActionSpecification, Finding, ImplementationStep, SourceType
from app.report_v22.public_action_catalog import (
    ACTION_TEMPLATES,
    CATALOG_VERSION,
    RULE_ACTIONS,
    ActionTemplate,
)
from app.report_v22.public_rule_catalog import ASSET, GBP_RULES, RULESET_VERSION


_SEVERITY_RANK = {"low": 1, "medium": 2, "high": 3, "critical": 4}
_CONFIDENCE_RANK = {"low": 1, "medium": 2, "high": 3}
_CLASSIFICATION_RANK = {"estimate": 1, "inference": 2, "fact": 3}
_SOURCE_ORDER: tuple[SourceType, ...] = (
    "site",
    "serp",
    "competitor",
    "gsc",
    "gbp",
    "ga4",
    "pagespeed",
    "coverage",
)
_TARGET_ORDER = {"site": 0, "url": 1, "query": 2, "page_type": 3, "gbp_field": 4}


@dataclass
class _Candidate:
    group_key: str
    template: ActionTemplate
    entries: list[tuple[Finding, RuleEvaluation]] = field(default_factory=list)
    targets: list[ActionTarget] = field(default_factory=list)
    action_id: str = ""
    candidate_key: str = ""
    anchor: Finding | None = None
    priority: ActionPriority | None = None
    data_sources: list[SourceType] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)


def _validated_input(value: PublicActionPlanInput | dict[str, Any]) -> PublicActionPlanInput:
    try:
        reject_nonfinite(value)
        raw = (
            value.model_dump(mode="json", warnings=False)
            if isinstance(value, PublicActionPlanInput)
            else value
        )
        return PublicActionPlanInput.model_validate_json(canonical_json_bytes(raw))
    except (TypeError, ValidationError, ValueError):
        raise PublicActionError("INPUT_INVALID") from None


def canonical_public_findings(value: PublicFindingsResult) -> PublicFindingsResult:
    """Return the stable findings representation used by downstream checksums."""
    evidence = value.evidence_result
    canonical_evidence = evidence.model_copy(
        update={
            "evidence_index": sorted(evidence.evidence_index, key=lambda item: item.evidence_id),
            "source_traces": sorted(evidence.source_traces, key=lambda item: item.evidence_id),
            "source_summaries": sorted(evidence.source_summaries, key=lambda item: str(item.snapshot_id)),
            "coverage_gaps": sorted(evidence.coverage_gaps, key=canonical_json_bytes),
        }
    )
    return value.model_copy(
        update={
            "evidence_result": canonical_evidence,
            "findings": sorted(value.findings, key=lambda item: item.finding_id),
            "rule_evaluations": sorted(
                value.rule_evaluations,
                key=lambda item: canonical_json_bytes(item),
            ),
            "cluster_rollups": sorted(
                value.cluster_rollups,
                key=lambda item: (item.page_type or "", canonical_json_bytes(item)),
            ),
        }
    )


def _validated_references(
    result: PublicFindingsResult,
    *,
    max_findings: int,
) -> tuple[dict[str, Finding], dict[str, RuleEvaluation], dict[str, SourceType]]:
    if result.ruleset_version != RULESET_VERSION:
        raise PublicActionError("UNSUPPORTED_FINDING")
    if len(result.findings) > max_findings:
        raise PublicActionError("LIMIT_EXCEEDED")
    findings = {item.finding_id: item for item in result.findings}
    if len(findings) != len(result.findings):
        raise PublicActionError("ID_CONFLICT")
    if result.site_rollup.finding_ids != sorted(findings):
        raise PublicActionError("REFERENCE_INVALID")
    evidence = {item.evidence_id: item for item in result.evidence_result.evidence_index}
    traces = {item.evidence_id: item for item in result.evidence_result.source_traces}
    if len(evidence) != len(result.evidence_result.evidence_index) or set(evidence) != set(traces):
        raise PublicActionError("REFERENCE_INVALID")
    evaluations: dict[str, RuleEvaluation] = {}
    for item in result.rule_evaluations:
        if item.state == "triggered" and item.finding_id is None:
            raise PublicActionError("REFERENCE_INVALID")
        if item.finding_id is None:
            continue
        if item.finding_id in evaluations:
            raise PublicActionError("REFERENCE_INVALID")
        evaluations[item.finding_id] = item
    if set(evaluations) != set(findings):
        raise PublicActionError("REFERENCE_INVALID")
    for key, finding in findings.items():
        evaluation = evaluations[key]
        expected_queries = [evaluation.target.query] if evaluation.target.query is not None else []
        if (
            evaluation.state != "triggered"
            or evaluation.rule_id != finding.rule_id
            or evaluation.rule_version != finding.rule_version
            or evaluation.evidence_ids != finding.evidence_ids
            or evaluation.comparator_ids != finding.comparator_ids
            or evaluation.target.urls != finding.affected_urls
            or expected_queries != finding.affected_queries
            or not set(finding.evidence_ids + finding.comparator_ids) <= set(evidence)
        ):
            raise PublicActionError("REFERENCE_INVALID")
        referenced = [evidence[item] for item in finding.evidence_ids + finding.comparator_ids]
        referenced_urls = {
            str(item.source_locator.url) for item in referenced if item.source_locator.url is not None
        }
        referenced_queries = {
            item.source_locator.query for item in referenced if item.source_locator.query is not None
        }
        if (
            not {str(url) for url in finding.affected_urls} <= referenced_urls
            or not set(finding.affected_queries) <= referenced_queries
        ):
            raise PublicActionError("REFERENCE_INVALID")
        binding = RULE_ACTIONS.get(finding.rule_id)
        if binding is None or binding.rule_version != finding.rule_version:
            raise PublicActionError("UNSUPPORTED_FINDING")
    return findings, evaluations, {key: item.source_type for key, item in evidence.items()}


def _group_key(finding: Finding, evaluation: RuleEvaluation) -> str:
    binding = RULE_ACTIONS[finding.rule_id]
    if finding.rule_id == ASSET:
        if evaluation.target.page_type is None:
            raise PublicActionError("REFERENCE_INVALID")
        return f"{binding.template_key}:{evaluation.target.page_type}"
    return binding.template_key


def _target_value(target: ActionTarget) -> str:
    if target.kind in {"site", "url"}:
        return str(target.url)
    if target.kind == "query":
        return target.query or ""
    if target.kind == "page_type":
        return target.page_type or ""
    return target.gbp_field or ""


def _target_sort_key(target: ActionTarget) -> tuple[int, str]:
    return _TARGET_ORDER[target.kind], _target_value(target)


def _build_targets(candidate: _Candidate, *, site_url: Any) -> list[ActionTarget]:
    references: dict[tuple[str, str], set[str]] = {}

    def add(kind: str, value: Any, finding_id: str) -> None:
        references.setdefault((kind, str(value)), set()).add(finding_id)

    for finding, evaluation in candidate.entries:
        for url in finding.affected_urls:
            add("url", url, finding.finding_id)
        for query in finding.affected_queries:
            add("query", query, finding.finding_id)
        if finding.rule_id == ASSET:
            if evaluation.target.page_type is None:
                raise PublicActionError("REFERENCE_INVALID")
            add("page_type", evaluation.target.page_type, finding.finding_id)
        if finding.rule_id in GBP_RULES:
            field_name = RULE_ACTIONS[finding.rule_id].gbp_field
            if field_name is None:
                raise PublicActionError("REFERENCE_INVALID")
            add("gbp_field", field_name, finding.finding_id)
    if candidate.template.key == "review_market_visibility":
        for finding, _ in candidate.entries:
            add("site", site_url, finding.finding_id)

    targets = []
    for (kind, value), finding_ids in references.items():
        payload: dict[str, Any] = {"kind": kind, "finding_ids": sorted(finding_ids)}
        if kind in {"site", "url"}:
            payload["url"] = value
        else:
            payload[kind] = value
        try:
            targets.append(ActionTarget.model_validate(payload))
        except ValidationError:
            raise PublicActionError("REFERENCE_INVALID") from None
    if not targets:
        raise PublicActionError("REFERENCE_INVALID")
    return sorted(targets, key=_target_sort_key)


def _finding_rank(finding: Finding) -> tuple[int, int, int, int, str]:
    impact = min(len(set(finding.affected_urls)), 5) + min(len(set(finding.affected_queries)), 5) + 1
    return (
        -_SEVERITY_RANK[finding.severity],
        -_CONFIDENCE_RANK[finding.confidence],
        -_CLASSIFICATION_RANK[finding.classification],
        -impact,
        finding.finding_id,
    )


def _candidate_priority(candidate: _Candidate) -> tuple[Finding, ActionPriority]:
    findings = [item[0] for item in candidate.entries]
    anchor = sorted(findings, key=_finding_rank)[0]
    urls = {str(url) for finding in findings for url in finding.affected_urls}
    queries = {query for finding in findings for query in finding.affected_queries}
    impact = min(len(urls), 5) + min(len(queries), 5) + min(len(findings), 5)
    return anchor, ActionPriority(
        severity_rank=_SEVERITY_RANK[anchor.severity],
        confidence_rank=_CONFIDENCE_RANK[anchor.confidence],
        classification_rank=_CLASSIFICATION_RANK[anchor.classification],
        impact_scope=impact,
    )


def _candidate_sort_key(candidate: _Candidate) -> tuple[Any, ...]:
    if candidate.priority is None:
        raise PublicActionError("REFERENCE_INVALID")
    priority = candidate.priority
    target_key = canonical_json_bytes(
        [{"kind": item.kind, "value": _target_value(item)} for item in candidate.targets]
    )
    return (
        -priority.severity_rank,
        -priority.confidence_rank,
        -priority.classification_rank,
        -priority.impact_scope,
        candidate.template.order,
        target_key,
        candidate.action_id,
    )


def _action_identity(candidate: _Candidate) -> tuple[str, str]:
    target_identity = [{"kind": item.kind, "value": _target_value(item)} for item in candidate.targets]
    digest = request_digest(
        {
            "catalog_version": CATALOG_VERSION,
            "template_key": candidate.template.key,
            "template_version": candidate.template.version,
            "targets": target_identity,
        }
    )[7:19]
    action_id = f"ac_{candidate.template.id_slug}_{digest}"
    return action_id, f"candidate-{digest}"


def _build_candidates(
    result: PublicFindingsResult,
    findings: dict[str, Finding],
    evaluations: dict[str, RuleEvaluation],
    evidence_sources: dict[str, SourceType],
    *,
    max_candidates: int,
    max_findings_per_action: int,
    max_targets_per_action: int,
) -> list[_Candidate]:
    groups: dict[str, _Candidate] = {}
    for key in sorted(findings):
        finding, evaluation = findings[key], evaluations[key]
        binding = RULE_ACTIONS[finding.rule_id]
        template = ACTION_TEMPLATES[binding.template_key]
        group_key = _group_key(finding, evaluation)
        candidate = groups.setdefault(group_key, _Candidate(group_key=group_key, template=template))
        candidate.entries.append((finding, evaluation))
    if len(groups) > max_candidates:
        raise PublicActionError("LIMIT_EXCEEDED")

    candidates = []
    for group_key in sorted(groups):
        candidate = groups[group_key]
        candidate.entries.sort(key=lambda item: item[0].finding_id)
        if len(candidate.entries) > max_findings_per_action:
            raise PublicActionError("LIMIT_EXCEEDED")
        candidate.targets = _build_targets(candidate, site_url=result.site_rollup.site_url)
        if len(candidate.targets) > max_targets_per_action:
            raise PublicActionError("LIMIT_EXCEEDED")
        actual_sources = {
            evidence_sources[key]
            for finding, _ in candidate.entries
            for key in finding.evidence_ids + finding.comparator_ids
        }
        allowed_sources = set(candidate.template.allowed_source_types)
        if not actual_sources or not actual_sources <= allowed_sources:
            raise PublicActionError("REFERENCE_INVALID")
        candidate.data_sources = [source for source in _SOURCE_ORDER if source in actual_sources]
        candidate.anchor, candidate.priority = _candidate_priority(candidate)
        candidate.action_id, candidate.candidate_key = _action_identity(candidate)
        candidates.append(candidate)
    action_ids = [item.action_id for item in candidates]
    if len(action_ids) != len(set(action_ids)):
        raise PublicActionError("ID_CONFLICT")
    return sorted(candidates, key=_candidate_sort_key)


def _url_targets(candidate: _Candidate) -> set[str]:
    return {str(item.url) for item in candidate.targets if item.kind == "url" and item.url is not None}


def _add_dependencies(selected: list[_Candidate], *, site_url: Any) -> None:
    recovery = next(
        (item for item in selected if item.template.key == "restore_site_access_indexing"),
        None,
    )
    if recovery is None:
        return
    recovery_urls = _url_targets(recovery)
    recovery_hosts = {host(url) for url in recovery_urls}
    for candidate in selected:
        if candidate is recovery:
            continue
        if candidate.template.key == "differentiate_page_titles" and recovery_urls.intersection(
            _url_targets(candidate)
        ):
            candidate.dependencies.append(recovery.action_id)
        elif candidate.template.key == "review_market_visibility" and host(site_url) in recovery_hosts:
            candidate.dependencies.append(recovery.action_id)


def _topological_order(selected: list[_Candidate]) -> list[_Candidate]:
    by_id = {item.action_id: item for item in selected}
    if len(by_id) != len(selected):
        raise PublicActionError("ID_CONFLICT")
    ordered: list[_Candidate] = []
    completed: set[str] = set()
    while len(ordered) < len(selected):
        available = [
            item
            for item in selected
            if item.action_id not in completed and set(item.dependencies) <= completed
        ]
        if not available:
            raise PublicActionError("DEPENDENCY_INVALID")
        current = sorted(available, key=_candidate_sort_key)[0]
        if current.action_id in current.dependencies or not set(current.dependencies) <= set(by_id):
            raise PublicActionError("DEPENDENCY_INVALID")
        ordered.append(current)
        completed.add(current.action_id)
    return ordered


def _build_action(
    candidate: _Candidate,
    *,
    sequence: int,
    review_date: Any,
    limits: Any,
) -> ActionSkeleton:
    template = candidate.template
    finding_ids = sorted(finding.finding_id for finding, _ in candidate.entries)
    if (
        len(template.steps) > limits.max_steps_per_action
        or max(
            len(template.content_requirements),
            len(template.gbp_requirements),
            len(template.technical_requirements),
        )
        > limits.max_requirements_per_group
        or len(template.required_client_assets) > limits.max_assets_per_action
        or 1 > limits.max_metrics_per_action
    ):
        raise PublicActionError("LIMIT_EXCEEDED")
    metric_sources = [
        source
        for source in _SOURCE_ORDER
        if source in candidate.data_sources and source in template.allowed_source_types
    ]
    if not metric_sources:
        raise PublicActionError("REFERENCE_INVALID")
    try:
        return ActionSkeleton(
            action_id=candidate.action_id,
            sequence=sequence,
            finding_ids=finding_ids,
            template_key=template.key,
            template_version=template.version,
            exact_targets=candidate.targets,
            implementation_steps=[
                ImplementationStep(sequence=index, title=title, instruction=instruction)
                for index, (title, instruction) in enumerate(template.steps, 1)
            ],
            specification=ActionSpecification(
                content_requirements=list(template.content_requirements),
                gbp_requirements=list(template.gbp_requirements),
                technical_requirements=list(template.technical_requirements),
            ),
            required_client_assets=list(template.required_client_assets),
            dependencies=sorted(set(candidate.dependencies)),
            owner_suggestion=template.owner_suggestion,
            effort_bucket=template.effort_bucket,
            definition_of_done=list(template.definition_of_done),
            validation_metrics=[
                ActionValidationMetric(
                    metric_key=template.metric_key,
                    baseline=template.metric_baseline,
                    success_condition=template.metric_success_condition,
                    source_types=metric_sources,
                )
            ],
            data_sources=candidate.data_sources,
            review_date=review_date,
            copy_requirements=ActionCopyRequirements(
                finding_ids=finding_ids,
                allowed_fact_fields=list(template.copy_allowed_fact_fields),
                required_limitations=list(template.copy_required_limitations),
            ),
        )
    except ValidationError:
        raise PublicActionError("REFERENCE_INVALID") from None


def build_public_action_plan(
    value: PublicActionPlanInput | dict[str, Any],
) -> PublicActionPlan:
    request = _validated_input(value)
    result = canonical_public_findings(request.findings_result)
    findings, evaluations, evidence_sources = _validated_references(
        result,
        max_findings=request.limits.max_findings,
    )
    candidates = _build_candidates(
        result,
        findings,
        evaluations,
        evidence_sources,
        max_candidates=request.limits.max_candidates,
        max_findings_per_action=request.limits.max_findings_per_action,
        max_targets_per_action=request.limits.max_targets_per_action,
    )
    if len(candidates) < 3:
        raise PublicActionError("INSUFFICIENT_ACTIONABLE_FINDINGS")
    if len(candidates) > request.limits.max_audit_entries:
        raise PublicActionError("LIMIT_EXCEEDED")

    selected = candidates[:3]
    _add_dependencies(selected, site_url=result.site_rollup.site_url)
    ordered = _topological_order(selected)
    actions = []
    for sequence, candidate in enumerate(ordered, 1):
        try:
            review_date = request.planning_date + timedelta(days=30 * sequence)
        except OverflowError:
            raise PublicActionError("DATE_INVALID") from None
        actions.append(
            _build_action(
                candidate,
                sequence=sequence,
                review_date=review_date,
                limits=request.limits,
            )
        )

    selected_ids = {item.action_id for item in selected}
    selected_findings = {
        finding.finding_id
        for item in selected
        for finding, _ in item.entries
    }
    audit = [
        ActionSelectionAudit(
            candidate_key=item.candidate_key,
            action_id=item.action_id,
            template_key=item.template.key,
            anchor_finding_id=item.anchor.finding_id if item.anchor is not None else "",
            finding_ids=sorted(finding.finding_id for finding, _ in item.entries),
            priority=item.priority,
            selection_state="selected" if item.action_id in selected_ids else "unselected",
            reason="selected_top_three" if item.action_id in selected_ids else "lower_priority",
        )
        for item in candidates
    ]
    try:
        plan = PublicActionPlan(
            planning_date=request.planning_date,
            findings_checksum=request_digest(result),
            actions=actions,
            selection_audit=audit,
            unselected_finding_ids=sorted(set(findings) - selected_findings),
        )
    except ValidationError:
        raise PublicActionError("REFERENCE_INVALID") from None
    if len(canonical_json_bytes(plan)) > request.limits.max_bytes:
        raise PublicActionError("LIMIT_EXCEEDED")
    return plan
