"""Dify narrative-only contract and backend report skeleton assembly."""

from __future__ import annotations

import copy as copy_module
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictStr, ValidationError

from app.report_v21.evidence_ledger import build_layer_evidence
from app.report_v21.action_requirements import (
    ACTION_REQUIREMENTS_BY_KEY,
    ActionRequirement,
    action_finding_details,
    active_action_requirements,
)
from app.report_v21.models import (
    LAYER_DISPLAY_LABELS,
    LAYER_LABELS,
    LayerKey,
    REQUIRED_LAYER_KEYS,
)
from app.report_v21.scoring import LAYER_RULES, calculate_layer_status


class _StrictCopyModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class CatalogActionCopy(_StrictCopyModel):
    action_key: str
    covers_finding_keys: list[str] = Field(min_length=1)
    priority: Literal["high", "medium", "low"]
    task_title: str
    affected_layer: LayerKey
    where_to_add: list[str] = Field(default_factory=list)
    what_to_add: list[str] = Field(default_factory=list)
    example_copy: list[StrictStr] = Field(default_factory=list)
    implementation_notes: list[str] = Field(default_factory=list)
    completion_signals: list[str] = Field(default_factory=list)
    expected_effect: str
    effort_level: Literal["small", "medium", "large"]


class LayerCopy(_StrictCopyModel):
    layer_key: LayerKey
    summary: str
    explanation: str
    suggested_fixes: list[str] = Field(default_factory=list)


class KeyIssueCopy(_StrictCopyModel):
    finding_keys: list[str] = Field(min_length=1)
    issue_title: str
    affected_layer: LayerKey
    judgement: str
    explanation: str
    why_it_matters: str
    impacts: list[str] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)
    recommended_action_keys: list[str] = Field(min_length=1, max_length=3)


class PageLevelCopy(_StrictCopyModel):
    current_assessment: str
    existing_foundation: str
    main_limitation: str
    likely_search_outcome: str
    competitive_interpretation: str


class RoadmapPhaseCopy(_StrictCopyModel):
    phase_title: str
    sequence: int = Field(ge=1)
    goal: str
    entry_condition: str
    action_keys: list[str] = Field(default_factory=list)
    expected_outcomes: list[str] = Field(default_factory=list)


class OptimizationCopy(_StrictCopyModel):
    must_execute_now_action_keys: list[str] = Field(default_factory=list)
    defer_until_later_action_keys: list[str] = Field(default_factory=list)
    do_not_prioritize_yet_action_keys: list[str] = Field(default_factory=list)
    roadmap: list[RoadmapPhaseCopy] = Field(default_factory=list)
    fix_order_warning: str
    completion_signals: list[str] = Field(default_factory=list)


class ClientSummaryCopy(_StrictCopyModel):
    title: str
    plain_language_summary: str
    why_it_matters: str
    first_priority: str
    not_first_priority: str
    expected_change: str


class ReportCopyV21(_StrictCopyModel):
    page_level: PageLevelCopy
    layers: list[LayerCopy] = Field(min_length=8, max_length=8)
    action_catalog: list[CatalogActionCopy] = Field(default_factory=list)
    key_issues: list[KeyIssueCopy] = Field(default_factory=list)
    optimization_path: OptimizationCopy
    client_summary: ClientSummaryCopy


class ReportCopyInvalid(RuntimeError):
    retryable = True
    error_code = "V21_COPY_INVALID"

    def __init__(self, errors: list[str]):
        super().__init__("Dify returned incomplete report narrative.")
        self.details = errors


def parse_report_copy(outputs: Any) -> ReportCopyV21:
    """Parse strict narrative output without accepting objective report fields."""
    if not isinstance(outputs, dict):
        raise ReportCopyInvalid(["Dify outputs must be an object."])
    value = outputs.get("report_copy_v2_1")
    parsed = _parse_json(value)
    if isinstance(parsed, dict) and isinstance(parsed.get("report_copy_v2_1"), dict):
        parsed = parsed["report_copy_v2_1"]
    try:
        return ReportCopyV21.model_validate(parsed)
    except ValidationError as exc:
        raise ReportCopyInvalid([str(error) for error in exc.errors()]) from exc


def validate_english_copy(copy: ReportCopyV21) -> None:
    """Reject Chinese characters in every Dify-owned user-visible string."""
    failures: list[str] = []

    def visit(value: Any, path: str) -> None:
        if isinstance(value, str) and re.search(r"[\u3400-\u4dbf\u4e00-\u9fff]", value):
            failures.append(path)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                visit(item, f"{path}[{index}]")
        elif isinstance(value, dict):
            for key, item in value.items():
                visit(item, f"{path}.{key}" if path else key)

    visit(copy.model_dump(mode="json"), "report_copy_v2_1")
    if failures:
        error = ReportCopyInvalid(failures[:20])
        error.error_code = "V21_LANGUAGE_INVALID"
        raise error


def assemble_report_skeleton(
    copy: ReportCopyV21,
    rule_results: dict[int, bool],
    rule_applicability: dict[int, bool],
    evidence_ledger: dict[str, dict[str, Any]],
    context: dict[str, Any],
) -> dict[str, Any]:
    """Build all objective report structure from the authoritative rule vector."""
    triggered = {
        rule_id
        for rule_id, value in rule_results.items()
        if value and rule_applicability.get(rule_id, True)
    }
    layer_copy_by_key = {item.layer_key: item for item in copy.layers}
    if set(layer_copy_by_key) != set(REQUIRED_LAYER_KEYS):
        raise ReportCopyInvalid(["layers must contain each fixed layer_key exactly once."])

    active_requirements = active_action_requirements(rule_results, rule_applicability)
    action_catalog = _validated_action_catalog(copy.action_catalog, active_requirements)

    layers: list[dict[str, Any]] = []
    layer_statuses: dict[str, str] = {}
    for index, layer_key in enumerate(REQUIRED_LAYER_KEYS, start=1):
        narrative = layer_copy_by_key[layer_key]
        triggered_ids = [rule_id for rule_id in LAYER_RULES[layer_key] if rule_id in triggered]
        status = calculate_layer_status(layer_key, triggered_ids)
        layer_statuses[layer_key] = status
        layer_actions = [
            copy_module.deepcopy(action)
            for action in action_catalog.values()
            if action["affected_layer"] == layer_key
        ]
        if status == "good" and not triggered_ids:
            presentation_mode = "healthy"
            suggested_fixes: list[str] = []
        elif status == "good":
            presentation_mode = "healthy_with_opportunities"
            suggested_fixes = _dedupe_strings([
                required_change
                for action in layer_actions
                for required_change in action.get("required_changes", [])
            ])
        else:
            presentation_mode = "attention"
            suggested_fixes = narrative.suggested_fixes
        layers.append({
            "layer_id": index,
            "layer_key": layer_key,
            "layer_name": LAYER_LABELS[layer_key],
            "layer_label": LAYER_DISPLAY_LABELS[layer_key],
            "status": status,
            "presentation_mode": presentation_mode,
            "checked_rule_ids": list(LAYER_RULES[layer_key]),
            "triggered_rule_ids": triggered_ids,
            "triggered_findings": [],
            "summary": narrative.summary,
            "explanation": narrative.explanation,
            "evidence_items": build_layer_evidence(
                triggered_ids,
                evidence_ledger,
                context,
            ),
            "suggested_fixes": suggested_fixes,
            "action_items": layer_actions,
        })

    issues: list[dict[str, Any]] = []
    seen_issue_action_keys: set[str] = set()
    for index, issue in enumerate(copy.key_issues, start=1):
        layer_key = issue.affected_layer
        status = layer_statuses[layer_key]
        unknown_issue_action_keys = sorted(
            set(issue.recommended_action_keys) - set(ACTION_REQUIREMENTS_BY_KEY)
        )
        if unknown_issue_action_keys:
            raise ReportCopyInvalid([
                f"Key Issue references unknown action keys: {unknown_issue_action_keys}."
            ])
        active_issue_action_keys = [
            action_key
            for action_key in issue.recommended_action_keys
            if action_key in action_catalog
        ]
        if not active_issue_action_keys:
            # Dify may draft narrative for a known catalog group that the
            # authoritative rule vector did not activate. It must not enter the
            # final report, but it also must not invalidate otherwise complete
            # active remediation coverage.
            continue
        if len(issue.recommended_action_keys) != 1:
            raise ReportCopyInvalid([
                "Every Key Issue must reference exactly one unified Action."
            ])
        action_key = active_issue_action_keys[0]
        action = action_catalog.get(action_key)
        if action is None:
            raise ReportCopyInvalid([
                f"Key Issue references unknown or inactive action_key {action_key}."
            ])
        if action_key in seen_issue_action_keys:
            raise ReportCopyInvalid([
                f"Action {action_key} may be referenced by only one Key Issue."
            ])
        if action["affected_layer"] != layer_key:
            raise ReportCopyInvalid([
                f"Action {action_key} must use affected_layer {action['affected_layer']}."
            ])
        expected_finding_keys = [f"rule_{rule_id}" for rule_id in action["related_rule_ids"]]
        if issue.finding_keys != expected_finding_keys:
            raise ReportCopyInvalid([
                f"Key Issue for {action_key} must use finding_keys {expected_finding_keys}."
            ])
        seen_issue_action_keys.add(action_key)
        triggered_ids = list(action["related_rule_ids"])
        issue_evidence = build_layer_evidence(triggered_ids, evidence_ledger, context)

        issues.append({
            "id": f"issue-{action_key}",
            "issue_title": issue.issue_title,
            "affected_layer": layer_key,
            "related_rule_ids": triggered_ids,
            "severity": _issue_severity(status),
            "evidence_items": issue_evidence,
            "judgement": issue.judgement,
            "explanation": issue.explanation,
            "why_it_matters": issue.why_it_matters,
            "impacts": issue.impacts,
            "suggestions": issue.suggestions,
            "recommended_actions": [copy_module.deepcopy(action)],
        })

    expected_issue_action_keys = set(action_catalog)
    if seen_issue_action_keys != expected_issue_action_keys:
        missing = sorted(expected_issue_action_keys - seen_issue_action_keys)
        extra = sorted(seen_issue_action_keys - expected_issue_action_keys)
        missing_findings = sorted({
            f"rule_{rule_id}"
            for action_key in missing
            for rule_id in action_catalog[action_key]["related_rule_ids"]
        })
        raise ReportCopyInvalid([
            "Every active remediation group requires exactly one Key Issue: "
            f"missing={missing}, missing_findings={missing_findings}, extra={extra}"
        ])

    blocker_key = _primary_blocker(triggered)
    blocker_layer = next(item for item in layers if item["layer_key"] == blocker_key)
    blocker_issue = next((item for item in issues if item["affected_layer"] == blocker_key), None)
    primary_reason = (
        str(blocker_issue.get("judgement"))
        if blocker_issue
        else blocker_layer["summary"]
    )

    return {
        "schema_version": "2.1",
        "report_id": "pending",
        "analyzed_url": str(context.get("url") or "unknown-url"),
        "page_type": str(context.get("page_type") or "unknown"),
        "generated_at": str(context.get("generated_at") or "pending"),
        "gbp_status": {"status": "not_checked", "source": "not_available", "reason": "Pending backend coverage."},
        "data_coverage": _placeholder_coverage(),
        "overall_status": {"label": "Trust Status", "level": "medium", "explanation": "Pending deterministic scoring."},
        "ranking_potential": {"label": "Ranking Potential", "level": "competitive", "explanation": "Pending deterministic scoring."},
        "risk_level": {"label": "Risk Level", "level": "medium", "explanation": "Pending deterministic scoring."},
        "primary_blocking_layer": {
            "layer_key": blocker_key,
            "layer_name": LAYER_LABELS[blocker_key],
            "reason": primary_reason,
            "evidence_items": blocker_layer["evidence_items"],
        },
        "page_level": {
            "label": "Pending",
            "what_it_looks_like": copy.page_level.current_assessment,
            "strengths": [],
            "missing_elements": [],
            **copy.page_level.model_dump(mode="json"),
        },
        "layers": layers,
        "key_issues": issues,
        "optimization_path": _optimization(
            copy.optimization_path,
            action_catalog,
        ),
        "client_summary": copy.client_summary.model_dump(mode="json"),
    }


def _validated_action_catalog(
    values: list[CatalogActionCopy],
    active_requirements: dict[str, tuple[ActionRequirement, tuple[int, ...]]],
) -> dict[str, dict[str, Any]]:
    incoming_by_key: dict[str, CatalogActionCopy] = {}
    for value in values:
        if value.action_key in incoming_by_key:
            raise ReportCopyInvalid([f"Duplicate action_key {value.action_key}."])
        incoming_by_key[value.action_key] = value

    expected_keys = set(active_requirements)
    incoming_keys = set(incoming_by_key)
    unknown_keys = incoming_keys - set(ACTION_REQUIREMENTS_BY_KEY)
    if unknown_keys:
        raise ReportCopyInvalid([
            f"Action catalog contains unknown action keys: {sorted(unknown_keys)}"
        ])
    missing_keys = expected_keys - incoming_keys
    if missing_keys:
        raise ReportCopyInvalid([
            "Action catalog must include every active backend remediation group: "
            f"missing={sorted(missing_keys)}"
        ])

    catalog: dict[str, dict[str, Any]] = {}
    covered_rule_ids: list[int] = []
    for action_key, (requirement, triggered_ids) in active_requirements.items():
        incoming = incoming_by_key[action_key]
        expected_finding_keys = [f"rule_{rule_id}" for rule_id in triggered_ids]
        errors: list[str] = []
        if incoming.covers_finding_keys != expected_finding_keys:
            errors.append(
                f"{action_key}.covers_finding_keys must be {expected_finding_keys}."
            )
        if incoming.affected_layer != requirement.affected_layer:
            errors.append(
                f"{action_key}.affected_layer must be {requirement.affected_layer}."
            )
        if incoming.priority != requirement.priority:
            errors.append(f"{action_key}.priority must be {requirement.priority}.")
        if incoming.effort_level != requirement.effort_level:
            errors.append(
                f"{action_key}.effort_level must be {requirement.effort_level}."
            )
        if errors:
            raise ReportCopyInvalid(errors)

        item = incoming.model_dump(
            mode="json",
            exclude={"action_key", "covers_finding_keys"},
        )
        item["id"] = f"act-{action_key}"
        item["related_rule_ids"] = list(triggered_ids)
        addressed_findings, required_changes = action_finding_details(triggered_ids)
        item["addressed_findings"] = addressed_findings
        item["required_changes"] = required_changes
        catalog[action_key] = item
        covered_rule_ids.extend(triggered_ids)

    if len(covered_rule_ids) != len(set(covered_rule_ids)):
        raise ReportCopyInvalid(["A triggered finding may be covered by only one unified Action."])
    return catalog


def _dedupe_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = value.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def _optimization(
    value: OptimizationCopy,
    action_catalog: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    known_keys = set(action_catalog)
    known_requirement_keys = set(ACTION_REQUIREMENTS_BY_KEY)
    supplied_groups = (
        value.must_execute_now_action_keys,
        value.defer_until_later_action_keys,
        value.do_not_prioritize_yet_action_keys,
    )
    supplied_keys = [key for group in supplied_groups for key in group]
    unknown_keys = sorted(set(supplied_keys) - known_requirement_keys)
    if unknown_keys:
        raise ReportCopyInvalid([
            f"Optimization Path references unknown Action keys: {unknown_keys}"
        ])

    layer_positions = {
        layer_key: index
        for index, layer_key in enumerate(REQUIRED_LAYER_KEYS, start=1)
    }
    ordered_keys = sorted(
        action_catalog,
        key=lambda key: (
            layer_positions[action_catalog[key]["affected_layer"]],
            key,
        ),
    )
    earliest_position = min(
        (
            layer_positions[action["affected_layer"]]
            for action in action_catalog.values()
        ),
        default=None,
    )
    must_keys: list[str] = []
    defer_keys: list[str] = []
    later_keys: list[str] = []
    for key in ordered_keys:
        position = layer_positions[action_catalog[key]["affected_layer"]]
        if position == earliest_position:
            must_keys.append(key)
        elif position <= 5:
            defer_keys.append(key)
        else:
            later_keys.append(key)

    def resolve(keys: list[str]) -> list[dict[str, Any]]:
        return [copy_module.deepcopy(action_catalog[key]) for key in keys]

    roadmap: list[dict[str, Any]] = []
    for index, phase in enumerate(value.roadmap, start=1):
        unknown_phase_keys = sorted(
            set(phase.action_keys) - known_requirement_keys
        )
        if unknown_phase_keys:
            raise ReportCopyInvalid([
                f"Roadmap phase {phase.sequence} references unknown Action keys: "
                f"{unknown_phase_keys}"
            ])
        active_phase_keys = [
            key
            for key in phase.action_keys
            if key in known_keys
        ]
        if not active_phase_keys:
            continue
        roadmap.append({
            "id": f"phase-{index:02d}",
            "phase_title": phase.phase_title,
            "sequence": phase.sequence,
            "goal": phase.goal,
            "entry_condition": phase.entry_condition,
            "action_items": resolve(active_phase_keys),
            "expected_outcomes": phase.expected_outcomes,
        })

    return {
        "must_execute_now": resolve(must_keys),
        "defer_until_later": resolve(defer_keys),
        "do_not_prioritize_yet": resolve(later_keys),
        "roadmap": roadmap,
        "fix_order_warning": value.fix_order_warning,
        "completion_signals": value.completion_signals,
    }


def _issue_severity(layer_status: str) -> str:
    if layer_status == "weak":
        return "high"
    if layer_status == "medium":
        return "medium"
    return "low"


def _primary_blocker(triggered: set[int]) -> str:
    for layer_key in REQUIRED_LAYER_KEYS:
        if any(rule_id in triggered for rule_id in LAYER_RULES[layer_key]):
            return layer_key
    return REQUIRED_LAYER_KEYS[0]


def _placeholder_coverage() -> dict[str, Any]:
    return {
        "page_content_checked": False,
        "gbp_checked": False,
        "schema_checked": False,
        "contact_page_checked": False,
        "about_page_checked": False,
        "reviews_checked": False,
        "internal_pages_checked": False,
        "competitor_pages_checked": False,
        "citations_checked": False,
        "geo_grid_checked": False,
        "limitations": ["Pending backend coverage."],
    }


def _parse_json(value: Any) -> Any:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        raise ReportCopyInvalid(["report_copy_v2_1 must be a JSON object or JSON string."])
    cleaned = re.sub(r"^```(?:json)?\s*", "", value.strip(), flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ReportCopyInvalid([f"report_copy_v2_1 is invalid JSON: {exc}"]) from exc
