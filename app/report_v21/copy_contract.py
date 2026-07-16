"""Dify narrative-only contract and backend report skeleton assembly."""

from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictStr, ValidationError

from app.report_v21.evidence_ledger import build_layer_evidence
from app.report_v21.models import (
    LAYER_DISPLAY_LABELS,
    LAYER_LABELS,
    LayerKey,
    REQUIRED_LAYER_KEYS,
)
from app.report_v21.scoring import LAYER_RULES, calculate_layer_status


class _StrictCopyModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ActionCopy(_StrictCopyModel):
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
    action_items: list[ActionCopy] = Field(default_factory=list)


class KeyIssueCopy(_StrictCopyModel):
    issue_title: str
    affected_layer: LayerKey
    judgement: str
    explanation: str
    why_it_matters: str
    impacts: list[str] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)
    recommended_actions: list[ActionCopy] = Field(default_factory=list)


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
    action_items: list[ActionCopy] = Field(default_factory=list)
    expected_outcomes: list[str] = Field(default_factory=list)


class OptimizationCopy(_StrictCopyModel):
    must_execute_now: list[ActionCopy] = Field(default_factory=list)
    defer_until_later: list[ActionCopy] = Field(default_factory=list)
    do_not_prioritize_yet: list[ActionCopy] = Field(default_factory=list)
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
    rule_evidence_ids: dict[int, list[str]],
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

    layers: list[dict[str, Any]] = []
    layer_statuses: dict[str, str] = {}
    for index, layer_key in enumerate(REQUIRED_LAYER_KEYS, start=1):
        narrative = layer_copy_by_key[layer_key]
        triggered_ids = [rule_id for rule_id in LAYER_RULES[layer_key] if rule_id in triggered]
        status = calculate_layer_status(layer_key, triggered_ids)
        layer_statuses[layer_key] = status
        layers.append({
            "layer_id": index,
            "layer_key": layer_key,
            "layer_name": LAYER_LABELS[layer_key],
            "layer_label": LAYER_DISPLAY_LABELS[layer_key],
            "status": status,
            "checked_rule_ids": list(LAYER_RULES[layer_key]),
            "triggered_rule_ids": triggered_ids,
            "triggered_findings": [],
            "summary": narrative.summary,
            "explanation": narrative.explanation,
            "evidence_items": build_layer_evidence(
                triggered_ids,
                rule_evidence_ids,
                evidence_ledger,
                context,
            ),
            "suggested_fixes": narrative.suggested_fixes,
            "action_items": _actions(narrative.action_items, triggered_ids, f"layer-{layer_key}"),
        })

    issues: list[dict[str, Any]] = []
    for index, issue in enumerate(copy.key_issues, start=1):
        layer_key = issue.affected_layer
        status = layer_statuses[layer_key]
        triggered_ids = [rule_id for rule_id in LAYER_RULES[layer_key] if rule_id in triggered]
        if status == "good" or not triggered_ids:
            continue
        layer = next(item for item in layers if item["layer_key"] == layer_key)
        issues.append({
            "id": f"issue-{index:02d}-{layer_key}",
            "issue_title": issue.issue_title,
            "affected_layer": layer_key,
            "related_rule_ids": triggered_ids,
            "severity": "high" if status == "weak" else "medium",
            "evidence_items": layer["evidence_items"],
            "judgement": issue.judgement,
            "explanation": issue.explanation,
            "why_it_matters": issue.why_it_matters,
            "impacts": issue.impacts,
            "suggestions": issue.suggestions,
            "recommended_actions": _actions(
                issue.recommended_actions,
                triggered_ids,
                f"issue-{index:02d}",
            ),
        })

    issue_layers = {issue["affected_layer"] for issue in issues}
    missing_weak = [key for key, status in layer_statuses.items() if status == "weak" and key not in issue_layers]
    if missing_weak:
        raise ReportCopyInvalid([
            "Every weak layer requires key issue narrative: " + ", ".join(missing_weak)
        ])

    blocker_key = _primary_blocker(layer_statuses)
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
        "optimization_path": _optimization(copy.optimization_path, triggered),
        "client_summary": copy.client_summary.model_dump(mode="json"),
    }


def _actions(
    values: list[ActionCopy],
    related_rule_ids: list[int],
    prefix: str,
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for index, action in enumerate(values, start=1):
        item = action.model_dump(mode="json")
        item["id"] = f"act-{prefix}-{index:02d}"
        item["related_rule_ids"] = [
            rule_id for rule_id in related_rule_ids if rule_id in LAYER_RULES[action.affected_layer]
        ]
        actions.append(item)
    return actions


def _optimization(value: OptimizationCopy, triggered: set[int]) -> dict[str, Any]:
    def action_group(items: list[ActionCopy], prefix: str) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for index, action in enumerate(items, start=1):
            rule_ids = [rule_id for rule_id in LAYER_RULES[action.affected_layer] if rule_id in triggered]
            result.extend(_actions([action], rule_ids, f"{prefix}-{index:02d}"))
        return result

    roadmap: list[dict[str, Any]] = []
    for index, phase in enumerate(value.roadmap, start=1):
        phase_actions: list[dict[str, Any]] = []
        for action_index, action in enumerate(phase.action_items, start=1):
            rule_ids = [rule_id for rule_id in LAYER_RULES[action.affected_layer] if rule_id in triggered]
            phase_actions.extend(_actions([action], rule_ids, f"roadmap-{index:02d}-{action_index:02d}"))
        roadmap.append({
            "id": f"phase-{index:02d}",
            "phase_title": phase.phase_title,
            "sequence": phase.sequence,
            "goal": phase.goal,
            "entry_condition": phase.entry_condition,
            "action_items": phase_actions,
            "expected_outcomes": phase.expected_outcomes,
        })

    return {
        "must_execute_now": action_group(value.must_execute_now, "must"),
        "defer_until_later": action_group(value.defer_until_later, "defer"),
        "do_not_prioritize_yet": action_group(value.do_not_prioritize_yet, "later"),
        "roadmap": roadmap,
        "fix_order_warning": value.fix_order_warning,
        "completion_signals": value.completion_signals,
    }


def _primary_blocker(statuses: dict[str, str]) -> str:
    for wanted in ("weak", "medium"):
        for layer_key in REQUIRED_LAYER_KEYS:
            if statuses.get(layer_key) == wanted:
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
