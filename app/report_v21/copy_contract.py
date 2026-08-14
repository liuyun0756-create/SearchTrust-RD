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
    # The final Dify node owns presentation copy, not the objective rule
    # vector.  Ignore forward-compatible fields and allow optional narrative
    # sections to be absent without rejecting an otherwise usable analysis.
    model_config = ConfigDict(extra="ignore", strict=True)


class CatalogActionCopy(_StrictCopyModel):
    action_key: str
    # Finding coverage is backend-owned and rebuilt from the authoritative
    # rule vector in ``_validated_action_catalog``. Dify may return narrative
    # templates for known but inactive actions, so an empty draft coverage
    # list must not invalidate an otherwise complete report.
    covers_finding_keys: list[str] = Field(default_factory=list)
    priority: Literal["high", "medium", "low"] = "medium"
    task_title: str = ""
    affected_layer: LayerKey = "foundation"
    where_to_add: list[str] = Field(default_factory=list)
    what_to_add: list[str] = Field(default_factory=list)
    example_copy: list[StrictStr] = Field(default_factory=list)
    implementation_notes: list[str] = Field(default_factory=list)
    completion_signals: list[str] = Field(default_factory=list)
    expected_effect: str = ""
    effort_level: Literal["small", "medium", "large"] = "medium"


class LayerCopy(_StrictCopyModel):
    layer_key: LayerKey
    summary: str = ""
    explanation: str = ""
    suggested_fixes: list[str] = Field(default_factory=list)


class KeyIssueCopy(_StrictCopyModel):
    finding_keys: list[str] = Field(default_factory=list)
    issue_title: str = ""
    affected_layer: LayerKey = "foundation"
    judgement: str = ""
    explanation: str = ""
    why_it_matters: str = ""
    impacts: list[str] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)
    recommended_action_keys: list[str] = Field(default_factory=list, max_length=3)


class PageLevelCopy(_StrictCopyModel):
    current_assessment: str = ""
    existing_foundation: str = ""
    main_limitation: str = ""
    likely_search_outcome: str = ""
    competitive_interpretation: str = ""


class RoadmapPhaseCopy(_StrictCopyModel):
    phase_title: str = ""
    sequence: int = Field(ge=1)
    goal: str = ""
    entry_condition: str = ""
    action_keys: list[str] = Field(default_factory=list)
    expected_outcomes: list[str] = Field(default_factory=list)


class OptimizationCopy(_StrictCopyModel):
    must_execute_now_action_keys: list[str] = Field(default_factory=list)
    defer_until_later_action_keys: list[str] = Field(default_factory=list)
    do_not_prioritize_yet_action_keys: list[str] = Field(default_factory=list)
    roadmap: list[RoadmapPhaseCopy] = Field(default_factory=list)
    fix_order_warning: str = ""
    completion_signals: list[str] = Field(default_factory=list)


class ClientSummaryCopy(_StrictCopyModel):
    title: str = ""
    plain_language_summary: str = ""
    why_it_matters: str = ""
    first_priority: str = ""
    not_first_priority: str = ""
    expected_change: str = ""


class ReportCopyV21(_StrictCopyModel):
    page_level: PageLevelCopy = Field(default_factory=PageLevelCopy)
    layers: list[LayerCopy] = Field(min_length=8, max_length=8)
    action_catalog: list[CatalogActionCopy] = Field(default_factory=list)
    key_issues: list[KeyIssueCopy] = Field(default_factory=list)
    optimization_path: OptimizationCopy = Field(default_factory=OptimizationCopy)
    client_summary: ClientSummaryCopy = Field(default_factory=ClientSummaryCopy)


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
    try:
        parsed = {} if value is None else _parse_json(value)
    except ReportCopyInvalid:
        # Narrative JSON is optional. The authoritative rule vector has
        # already been parsed separately, so malformed presentation copy may
        # safely degrade to deterministic backend copy.
        parsed = {}
    if isinstance(parsed, dict) and isinstance(parsed.get("report_copy_v2_1"), dict):
        parsed = parsed["report_copy_v2_1"]
    parsed = _complete_report_copy(parsed)
    try:
        return ReportCopyV21.model_validate(parsed)
    except ValidationError as exc:
        raise ReportCopyInvalid([str(error) for error in exc.errors()]) from exc


def _complete_report_copy(value: Any) -> Any:
    """Fill only presentation structure that is safe to derive locally.

    The rule vector remains mandatory and is validated before this function is
    reached.  Missing prose must not turn a completed rule assessment into a
    failed report.
    """
    if not isinstance(value, dict):
        return value

    completed = copy_module.deepcopy(value)
    raw_layers = completed.get("layers")
    by_key: dict[str, dict[str, Any]] = {}
    if isinstance(raw_layers, list):
        for item in raw_layers:
            if not isinstance(item, dict):
                continue
            layer_key = item.get("layer_key")
            if layer_key in REQUIRED_LAYER_KEYS and layer_key not in by_key:
                by_key[layer_key] = item
    completed["layers"] = []
    for layer_key in REQUIRED_LAYER_KEYS:
        incoming_layer = by_key.get(layer_key, {})
        completed["layers"].append({
            "layer_key": layer_key,
            "summary": _copy_text(incoming_layer.get("summary")),
            "explanation": _copy_text(incoming_layer.get("explanation")),
            "suggested_fixes": _copy_string_list(incoming_layer.get("suggested_fixes")),
        })

    page_level = completed.get("page_level")
    page_level = page_level if isinstance(page_level, dict) else {}
    completed["page_level"] = {
        key: _copy_text(page_level.get(key))
        for key in (
            "current_assessment",
            "existing_foundation",
            "main_limitation",
            "likely_search_outcome",
            "competitive_interpretation",
        )
    }

    raw_actions = completed.get("action_catalog")
    completed["action_catalog"] = [
        _normalize_action_copy(item)
        for item in raw_actions
        if isinstance(item, dict) and _copy_text(item.get("action_key"))
    ] if isinstance(raw_actions, list) else []

    raw_issues = completed.get("key_issues")
    completed["key_issues"] = [
        _normalize_issue_copy(item)
        for item in raw_issues
        if isinstance(item, dict)
    ] if isinstance(raw_issues, list) else []

    optimization = completed.get("optimization_path")
    completed["optimization_path"] = _normalize_optimization_copy(
        optimization if isinstance(optimization, dict) else {}
    )
    client_summary = completed.get("client_summary")
    client_summary = client_summary if isinstance(client_summary, dict) else {}
    completed["client_summary"] = {
        key: _copy_text(client_summary.get(key))
        for key in (
            "title",
            "plain_language_summary",
            "why_it_matters",
            "first_priority",
            "not_first_priority",
            "expected_change",
        )
    }
    return completed


def _normalize_action_copy(value: dict[str, Any]) -> dict[str, Any]:
    priority = value.get("priority")
    affected_layer = value.get("affected_layer")
    effort_level = value.get("effort_level")
    return {
        "action_key": _copy_text(value.get("action_key")),
        "covers_finding_keys": _copy_string_list(value.get("covers_finding_keys")),
        "priority": priority if priority in {"high", "medium", "low"} else "medium",
        "task_title": _copy_text(value.get("task_title")),
        "affected_layer": affected_layer if affected_layer in REQUIRED_LAYER_KEYS else "foundation",
        "where_to_add": _copy_string_list(value.get("where_to_add")),
        "what_to_add": _copy_string_list(value.get("what_to_add")),
        "example_copy": _copy_string_list(value.get("example_copy")),
        "implementation_notes": _copy_string_list(value.get("implementation_notes")),
        "completion_signals": _copy_string_list(value.get("completion_signals")),
        "expected_effect": _copy_text(value.get("expected_effect")),
        "effort_level": effort_level if effort_level in {"small", "medium", "large"} else "medium",
    }


def _normalize_issue_copy(value: dict[str, Any]) -> dict[str, Any]:
    affected_layer = value.get("affected_layer")
    return {
        "finding_keys": _copy_string_list(value.get("finding_keys")),
        "issue_title": _copy_text(value.get("issue_title")),
        "affected_layer": affected_layer if affected_layer in REQUIRED_LAYER_KEYS else "foundation",
        "judgement": _copy_text(value.get("judgement")),
        "explanation": _copy_text(value.get("explanation")),
        "why_it_matters": _copy_text(value.get("why_it_matters")),
        "impacts": _copy_string_list(value.get("impacts")),
        "suggestions": _copy_string_list(value.get("suggestions")),
        "recommended_action_keys": _copy_string_list(
            value.get("recommended_action_keys")
        )[:3],
    }


def _normalize_optimization_copy(value: dict[str, Any]) -> dict[str, Any]:
    raw_roadmap = value.get("roadmap")
    roadmap: list[dict[str, Any]] = []
    if isinstance(raw_roadmap, list):
        for index, item in enumerate(raw_roadmap, start=1):
            if not isinstance(item, dict):
                continue
            sequence = item.get("sequence")
            roadmap.append({
                "phase_title": _copy_text(item.get("phase_title")),
                "sequence": sequence if isinstance(sequence, int) and sequence >= 1 else index,
                "goal": _copy_text(item.get("goal")),
                "entry_condition": _copy_text(item.get("entry_condition")),
                "action_keys": _copy_string_list(item.get("action_keys")),
                "expected_outcomes": _copy_string_list(item.get("expected_outcomes")),
            })
    return {
        "must_execute_now_action_keys": _copy_string_list(value.get("must_execute_now_action_keys")),
        "defer_until_later_action_keys": _copy_string_list(value.get("defer_until_later_action_keys")),
        "do_not_prioritize_yet_action_keys": _copy_string_list(value.get("do_not_prioritize_yet_action_keys")),
        "roadmap": roadmap,
        "fix_order_warning": _copy_text(value.get("fix_order_warning")),
        "completion_signals": _copy_string_list(value.get("completion_signals")),
    }


def _copy_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _copy_string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


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
        checked_ids = (
            [
                rule_id
                for rule_id in LAYER_RULES[layer_key]
                if rule_applicability.get(rule_id, True)
            ]
            if layer_key == "entity_consistency"
            else list(LAYER_RULES[layer_key])
        )
        status = (
            calculate_layer_status(layer_key, triggered_ids)
            if checked_ids
            else "not_checked"
        )
        layer_statuses[layer_key] = status
        layer_actions = [
            copy_module.deepcopy(action)
            for action in action_catalog.values()
            if action["affected_layer"] == layer_key
        ]
        if status == "not_checked":
            presentation_mode = "attention"
            suggested_fixes = []
        elif status == "good" and not triggered_ids:
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
        if status == "not_checked":
            fallback_summary = "No page-to-GBP entity field could be compared in this audit."
            fallback_explanation = (
                "L3 requires at least one comparable field from both the checked page and GBP record."
            )
        elif triggered_ids:
            fallback_summary = "The audit identified confirmed opportunities in this trust layer."
            fallback_explanation = "The triggered assessment signals indicate that this layer should be strengthened."
        else:
            fallback_summary = "The assessed signals did not identify a material issue in this trust layer."
            fallback_explanation = (
                f"No material conflict was found among the {len(checked_ids)} assessed "
                f"entity {'field' if len(checked_ids) == 1 else 'fields'}."
                if layer_key == "entity_consistency"
                else "The checked signals currently support this layer."
            )
        if status != "good" and not suggested_fixes:
            suggested_fixes = _dedupe_strings([
                required_change
                for action in layer_actions
                for required_change in action.get("required_changes", [])
            ])
        layers.append({
            "layer_id": index,
            "layer_key": layer_key,
            "layer_name": LAYER_LABELS[layer_key],
            "layer_label": LAYER_DISPLAY_LABELS[layer_key],
            "status": status,
            "presentation_mode": presentation_mode,
            "checked_rule_ids": checked_ids,
            "triggered_rule_ids": triggered_ids,
            "triggered_findings": [],
            "summary": (
                fallback_summary
                if status == "not_checked"
                else narrative.summary.strip() or fallback_summary
            ),
            "explanation": (
                fallback_explanation
                if status == "not_checked"
                else narrative.explanation.strip() or fallback_explanation
            ),
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
        # Dify may consolidate closely related remediation groups into one
        # narrative issue. Preserve that narrative while projecting one stable
        # backend issue per authoritative action group.
        for action_key in active_issue_action_keys:
            action = action_catalog.get(action_key)
            if action is None:
                continue
            layer_key = action["affected_layer"]
            status = layer_statuses[layer_key]
            if action_key in seen_issue_action_keys:
                continue
            seen_issue_action_keys.add(action_key)
            triggered_ids = list(action["related_rule_ids"])
            issue_evidence = build_layer_evidence(triggered_ids, evidence_ledger, context)

            issues.append({
                "id": f"issue-{action_key}",
                "issue_title": issue.issue_title.strip() or action["task_title"],
                "affected_layer": layer_key,
                "related_rule_ids": triggered_ids,
                "severity": _issue_severity(status),
                "evidence_items": issue_evidence,
                "judgement": issue.judgement.strip() or "The audit confirmed this remediation group.",
                "explanation": issue.explanation.strip() or "The related trust findings require attention.",
                "why_it_matters": issue.why_it_matters.strip() or action["expected_effect"],
                "impacts": issue.impacts,
                "suggestions": issue.suggestions,
                "recommended_actions": [copy_module.deepcopy(action)],
            })

    expected_issue_action_keys = set(action_catalog)
    # If Dify omits a separate Key Issue for an otherwise complete active
    # action, build a conservative issue from backend-owned findings and the
    # validated Dify action narrative. This keeps a successful structured
    # report usable without inventing diagnostic facts.
    for action_key in sorted(expected_issue_action_keys - seen_issue_action_keys):
        action = action_catalog[action_key]
        layer_key = action["affected_layer"]
        triggered_ids = list(action["related_rule_ids"])
        issues.append({
            "id": f"issue-{action_key}",
            "issue_title": action["task_title"],
            "affected_layer": layer_key,
            "related_rule_ids": triggered_ids,
            "severity": _issue_severity(layer_statuses[layer_key]),
            "evidence_items": build_layer_evidence(
                triggered_ids,
                evidence_ledger,
                context,
            ),
            "judgement": "The audit confirmed this remediation group.",
            "explanation": "The related trust findings require the validated action below.",
            "why_it_matters": action["expected_effect"],
            "impacts": list(action.get("addressed_findings", [])),
            "suggestions": list(action.get("required_changes", [])),
            "recommended_actions": [copy_module.deepcopy(action)],
        })

    blocker_key = _primary_blocker(triggered)
    blocker_layer = next(item for item in layers if item["layer_key"] == blocker_key)
    blocker_issue = next((item for item in issues if item["affected_layer"] == blocker_key), None)
    primary_reason = (
        str(blocker_issue.get("judgement"))
        if blocker_issue
        else blocker_layer["summary"]
    )
    first_blocker_action = next(
        (
            action
            for action in action_catalog.values()
            if action["affected_layer"] == blocker_key
        ),
        None,
    )
    page_level_copy = copy.page_level.model_dump(mode="json")
    page_level_copy["current_assessment"] = (
        page_level_copy["current_assessment"]
        or "The audit identified confirmed trust opportunities that should be addressed in layer order."
    )
    page_level_copy["existing_foundation"] = (
        page_level_copy["existing_foundation"]
        or "The checked page provides enough service context to complete the trust assessment."
    )
    page_level_copy["main_limitation"] = (
        page_level_copy["main_limitation"]
        or f"The earliest confirmed constraint is {LAYER_LABELS[blocker_key]}."
    )
    page_level_copy["likely_search_outcome"] = (
        page_level_copy["likely_search_outcome"]
        or "Performance may remain less stable until the confirmed trust gaps are repaired."
    )
    page_level_copy["competitive_interpretation"] = (
        page_level_copy["competitive_interpretation"]
        or "Pages with clearer verified trust signals may be easier to interpret and defend."
    )
    client_summary = copy.client_summary.model_dump(mode="json")
    client_summary["title"] = (
        client_summary["title"]
        or f"Strengthen {LAYER_LABELS[blocker_key]} first"
    )
    client_summary["plain_language_summary"] = (
        client_summary["plain_language_summary"]
        or "The audit found confirmed trust gaps that should be repaired from the earliest affected layer upward."
    )
    client_summary["why_it_matters"] = (
        client_summary["why_it_matters"]
        or "Later optimization is less dependable while earlier trust signals remain incomplete."
    )
    client_summary["first_priority"] = (
        client_summary["first_priority"]
        or str((first_blocker_action or {}).get("task_title") or primary_reason)
    )
    client_summary["not_first_priority"] = (
        client_summary["not_first_priority"]
        or "Do not begin with broad expansion before the earliest confirmed trust gap is addressed."
    )
    client_summary["expected_change"] = (
        client_summary["expected_change"]
        or "The page should become easier to verify and support with specific trust signals."
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
            "what_it_looks_like": page_level_copy["current_assessment"],
            "strengths": [],
            "missing_elements": [],
            **page_level_copy,
        },
        "layers": layers,
        "key_issues": issues,
        "optimization_path": _optimization(
            copy.optimization_path,
            action_catalog,
        ),
        "client_summary": client_summary,
    }


def _validated_action_catalog(
    values: list[CatalogActionCopy],
    active_requirements: dict[str, tuple[ActionRequirement, tuple[int, ...]]],
) -> dict[str, dict[str, Any]]:
    incoming_by_key: dict[str, CatalogActionCopy] = {}
    for value in values:
        if value.action_key not in ACTION_REQUIREMENTS_BY_KEY:
            continue
        incoming_by_key.setdefault(value.action_key, value)

    catalog: dict[str, dict[str, Any]] = {}
    for action_key, (requirement, triggered_ids) in active_requirements.items():
        incoming = incoming_by_key.get(action_key)
        addressed_findings, required_changes = action_finding_details(triggered_ids)
        # Dify owns the action narrative; the backend owns deterministic
        # classification and finding coverage for each known action key.  If
        # Dify omitted an action, use the fixed remediation requirement rather
        # than rejecting and rerunning the entire workflow.
        item = (
            incoming.model_dump(
                mode="json",
                exclude={
                    "action_key",
                    "covers_finding_keys",
                    "priority",
                    "affected_layer",
                    "effort_level",
                },
            )
            if incoming is not None
            else {}
        )
        item["id"] = f"act-{action_key}"
        item["priority"] = requirement.priority
        item["affected_layer"] = requirement.affected_layer
        item["effort_level"] = requirement.effort_level
        item["related_rule_ids"] = list(triggered_ids)
        item["addressed_findings"] = addressed_findings
        item["required_changes"] = required_changes
        item["task_title"] = str(item.get("task_title") or requirement.task_goal)
        item["where_to_add"] = list(item.get("where_to_add") or [])
        item["what_to_add"] = list(item.get("what_to_add") or required_changes)
        item["example_copy"] = list(item.get("example_copy") or [])
        item["implementation_notes"] = list(item.get("implementation_notes") or [])
        item["completion_signals"] = list(item.get("completion_signals") or [])
        item["expected_effect"] = str(
            item.get("expected_effect")
            or f"Addresses the confirmed {LAYER_LABELS[requirement.affected_layer]} trust findings."
        )
        catalog[action_key] = item

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
    for key in ordered_keys:
        position = layer_positions[action_catalog[key]["affected_layer"]]
        if position == earliest_position:
            must_keys.append(key)
        else:
            # Every confirmed finding belongs to the four-stage implementation
            # plan. Later layers are scheduled after the current phase rather
            # than described as work that should not be prioritized.
            defer_keys.append(key)

    def resolve(keys: list[str]) -> list[dict[str, Any]]:
        return [copy_module.deepcopy(action_catalog[key]) for key in keys]

    roadmap: list[dict[str, Any]] = []
    for index, phase in enumerate(value.roadmap, start=1):
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

    if not roadmap and ordered_keys:
        phase_definitions = (
            (1, {"foundation", "entity_presence", "entity_consistency"}, "Stabilize the business entity"),
            (2, {"specificity", "real_world_connection"}, "Build local credibility"),
            (3, {"accountability", "page_unique_value"}, "Add accountable, unique proof"),
            (4, {"algorithm_fit"}, "Reassess search-era fit"),
        )
        for sequence, layer_keys, title in phase_definitions:
            phase_keys = [
                key
                for key in ordered_keys
                if action_catalog[key]["affected_layer"] in layer_keys
            ]
            if not phase_keys:
                continue
            roadmap.append({
                "id": f"phase-{sequence:02d}",
                "phase_title": title,
                "sequence": sequence,
                "goal": title,
                "entry_condition": "The related findings have been confirmed by the audit.",
                "action_items": resolve(phase_keys),
                "expected_outcomes": [
                    "The confirmed trust findings in this phase are addressed and verified."
                ],
            })

    return {
        "must_execute_now": resolve(must_keys),
        "defer_until_later": resolve(defer_keys),
        "do_not_prioritize_yet": [],
        "roadmap": roadmap,
        "fix_order_warning": value.fix_order_warning or "Repair earlier affected layers before expanding later-stage optimization.",
        "completion_signals": value.completion_signals or [
            "Each confirmed action is implemented and verified on the checked page."
        ],
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
