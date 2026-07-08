"""Deterministic dedupe helpers for report_v2_1 payloads."""

from __future__ import annotations

import copy
import re
from difflib import SequenceMatcher
from typing import Any


SEVERITY_RANK = {"high": 3, "medium": 2, "low": 1}
PRIORITY_RANK = {"high": 3, "medium": 2, "low": 1}
CONFIDENCE_RANK = {"high": 3, "medium": 2, "low": 1}
SIMILARITY_THRESHOLD = 0.88
LONG_TEXT_THRESHOLD = 120


def dedupe_report_v21(report_v2_1: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Return a deduped deep copy and deterministic warnings."""
    report = copy.deepcopy(report_v2_1)
    warnings: list[str] = []

    primary = report.get("primary_blocking_layer")
    if isinstance(primary, dict):
        primary["evidence_items"] = _dedupe_evidence_items(
            primary.get("evidence_items"),
            "primary_blocking_layer.evidence_items",
            warnings,
        )

    layers = report.get("layers")
    if isinstance(layers, list):
        for layer in layers:
            if not isinstance(layer, dict):
                continue
            layer_key = str(layer.get("layer_key") or layer.get("layer_name") or "unknown")
            layer["evidence_items"] = _dedupe_evidence_items(
                layer.get("evidence_items"),
                f"layers.{layer_key}.evidence_items",
                warnings,
            )
            layer["action_items"] = _dedupe_action_items(
                layer.get("action_items"),
                f"layers.{layer_key}.action_items",
                warnings,
            )

    key_issues = report.get("key_issues")
    if isinstance(key_issues, list):
        for issue in key_issues:
            if not isinstance(issue, dict):
                continue
            title = str(issue.get("issue_title") or issue.get("id") or "unknown")
            issue["evidence_items"] = _dedupe_evidence_items(
                issue.get("evidence_items"),
                f"key_issues.{title}.evidence_items",
                warnings,
            )
            issue["recommended_actions"] = _dedupe_action_items(
                issue.get("recommended_actions"),
                f"key_issues.{title}.recommended_actions",
                warnings,
            )
        report["key_issues"] = _dedupe_key_issues(key_issues, warnings)

    optimization = report.get("optimization_path")
    if isinstance(optimization, dict):
        for section in ("must_execute_now", "defer_until_later", "do_not_prioritize_yet"):
            optimization[section] = _dedupe_action_items(
                optimization.get(section),
                f"optimization_path.{section}",
                warnings,
            )
        optimization["roadmap"] = _dedupe_roadmap_phases(optimization.get("roadmap"), warnings)

    _dedupe_repeated_long_strings_in_lists(report, set())
    return report, _dedupe_strings(warnings)


def _dedupe_key_issues(items: Any, warnings: list[str]) -> list[Any]:
    if not isinstance(items, list):
        return []

    result: list[Any] = []
    for item in items:
        if not isinstance(item, dict):
            result.append(item)
            continue
        match_index = _find_match_index(result, item, _key_issues_duplicate)
        if match_index is None:
            result.append(item)
            continue
        result[match_index] = _merge_key_issue(result[match_index], item)
        warnings.append(f"Duplicate key issue merged: {item.get('issue_title', 'Untitled issue')}")
    return result


def _dedupe_action_items(items: Any, section_label: str, warnings: list[str]) -> list[Any]:
    if not isinstance(items, list):
        return []

    result: list[Any] = []
    for item in items:
        if not isinstance(item, dict):
            result.append(item)
            continue
        match_index = _find_match_index(result, item, _actions_duplicate)
        if match_index is None:
            result.append(item)
            continue
        result[match_index] = _merge_action_item(result[match_index], item)
        warnings.append(
            f"Duplicate action item merged in {section_label}: {item.get('task_title', 'Untitled action')}"
        )
    return result


def _dedupe_roadmap_phases(items: Any, warnings: list[str]) -> list[Any]:
    if not isinstance(items, list):
        return []

    result: list[Any] = []
    for item in items:
        if not isinstance(item, dict):
            result.append(item)
            continue
        match_index = _find_match_index(result, item, _roadmap_phases_duplicate)
        if match_index is None:
            item["action_items"] = _dedupe_action_items(
                item.get("action_items"),
                f"roadmap.{item.get('phase_title', 'Untitled phase')}.action_items",
                warnings,
            )
            result.append(item)
            continue
        result[match_index] = _merge_roadmap_phase(result[match_index], item, warnings)
        warnings.append(f"Duplicate roadmap phase merged: {item.get('phase_title', 'Untitled phase')}")
    return result


def _dedupe_evidence_items(items: Any, section_label: str, warnings: list[str]) -> list[Any]:
    if not isinstance(items, list):
        return []

    result: list[Any] = []
    removed_count = 0
    for item in items:
        if not isinstance(item, dict):
            result.append(item)
            continue
        match_index = _find_match_index(result, item, _evidence_duplicate)
        if match_index is None:
            result.append(item)
            continue
        result[match_index] = _merge_evidence_item(result[match_index], item)
        removed_count += 1

    if removed_count > 3:
        warnings.append(f"Duplicate evidence items removed in {section_label}: {removed_count}")
    return result


def _key_issues_duplicate(existing: dict[str, Any], candidate: dict[str, Any]) -> bool:
    return (
        _normalize_text(existing.get("issue_title")) == _normalize_text(candidate.get("issue_title"))
        and existing.get("affected_layer") == candidate.get("affected_layer")
        and _rule_ids_overlap_or_missing(existing.get("related_rule_ids"), candidate.get("related_rule_ids"))
    )


def _actions_duplicate(existing: dict[str, Any], candidate: dict[str, Any]) -> bool:
    if _normalize_text(existing.get("task_title")) != _normalize_text(candidate.get("task_title")):
        return False
    if existing.get("affected_layer") != candidate.get("affected_layer"):
        return False
    if not _rule_ids_overlap_or_missing(existing.get("related_rule_ids"), candidate.get("related_rule_ids")):
        return False

    existing_what = _joined_list_text(existing.get("what_to_add"))
    candidate_what = _joined_list_text(candidate.get("what_to_add"))
    if not existing_what or not candidate_what:
        return True
    return existing_what == candidate_what or _similar(existing_what, candidate_what)


def _roadmap_phases_duplicate(existing: dict[str, Any], candidate: dict[str, Any]) -> bool:
    if _normalize_text(existing.get("phase_title")) == _normalize_text(candidate.get("phase_title")):
        return True
    if existing.get("sequence") == candidate.get("sequence"):
        return True

    existing_goal = _normalize_text(existing.get("goal"))
    candidate_goal = _normalize_text(candidate.get("goal"))
    return bool(existing_goal and candidate_goal and _similar(existing_goal, candidate_goal))


def _evidence_duplicate(existing: dict[str, Any], candidate: dict[str, Any]) -> bool:
    keys = ("source_type", "source_url", "page_section", "extracted_text", "comparison_result")
    return all(_normalize_text(existing.get(key)) == _normalize_text(candidate.get(key)) for key in keys)


def _merge_key_issue(existing: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    keep, other = _choose_ranked(existing, candidate, "severity", SEVERITY_RANK)
    merged = copy.deepcopy(keep)
    merged["related_rule_ids"] = _merge_ints(existing.get("related_rule_ids"), candidate.get("related_rule_ids"))
    merged["evidence_items"] = _dedupe_evidence_items(
        [*_list(existing.get("evidence_items")), *_list(candidate.get("evidence_items"))],
        "merged key issue evidence",
        [],
    )
    merged["recommended_actions"] = _dedupe_action_items(
        [*_list(existing.get("recommended_actions")), *_list(candidate.get("recommended_actions"))],
        "merged key issue recommended_actions",
        [],
    )
    if not str(merged.get("explanation") or "").strip():
        merged["explanation"] = other.get("explanation", "")
    if not str(merged.get("why_it_matters") or "").strip():
        merged["why_it_matters"] = other.get("why_it_matters", "")
    return merged


def _merge_action_item(existing: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    keep, _ = _choose_ranked(existing, candidate, "priority", PRIORITY_RANK)
    merged = copy.deepcopy(keep)
    merged["related_rule_ids"] = _merge_ints(existing.get("related_rule_ids"), candidate.get("related_rule_ids"))
    for field in ("where_to_add", "what_to_add", "implementation_notes", "completion_signals"):
        merged[field] = _merge_strings(existing.get(field), candidate.get(field))
    if not str(merged.get("example_copy") or "").strip():
        merged["example_copy"] = str(existing.get("example_copy") or candidate.get("example_copy") or "")
    return merged


def _merge_roadmap_phase(
    existing: dict[str, Any],
    candidate: dict[str, Any],
    warnings: list[str],
) -> dict[str, Any]:
    existing_seq = _int_or_large(existing.get("sequence"))
    candidate_seq = _int_or_large(candidate.get("sequence"))
    keep, other = (existing, candidate) if existing_seq <= candidate_seq else (candidate, existing)
    merged = copy.deepcopy(keep)
    merged["action_items"] = _dedupe_action_items(
        [*_list(existing.get("action_items")), *_list(candidate.get("action_items"))],
        f"roadmap.{merged.get('phase_title', 'merged phase')}.action_items",
        warnings,
    )
    merged["expected_outcomes"] = _merge_strings(existing.get("expected_outcomes"), candidate.get("expected_outcomes"))
    if len(str(other.get("entry_condition") or "")) > len(str(merged.get("entry_condition") or "")):
        merged["entry_condition"] = other.get("entry_condition", "")
    return merged


def _merge_evidence_item(existing: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    keep, other = _choose_ranked(existing, candidate, "confidence", CONFIDENCE_RANK)
    if CONFIDENCE_RANK.get(str(existing.get("confidence")), 0) == CONFIDENCE_RANK.get(
        str(candidate.get("confidence")), 0
    ):
        keep, other = _choose_more_complete(existing, candidate)
    merged = copy.deepcopy(keep)
    for key, value in other.items():
        if not _has_value(merged.get(key)) and _has_value(value):
            merged[key] = copy.deepcopy(value)
    return merged


def _dedupe_repeated_long_strings_in_lists(value: Any, seen: set[str]) -> None:
    if isinstance(value, list):
        kept: list[Any] = []
        for item in value:
            if isinstance(item, str) and len(item.strip()) > LONG_TEXT_THRESHOLD:
                if item in seen:
                    continue
                seen.add(item)
            _dedupe_repeated_long_strings_in_lists(item, seen)
            kept.append(item)
        value[:] = kept
        return
    if isinstance(value, dict):
        for item in value.values():
            _dedupe_repeated_long_strings_in_lists(item, seen)


def _find_match_index(
    items: list[Any],
    candidate: dict[str, Any],
    matcher: Any,
) -> int | None:
    for index, item in enumerate(items):
        if isinstance(item, dict) and matcher(item, candidate):
            return index
    return None


def _choose_ranked(
    existing: dict[str, Any],
    candidate: dict[str, Any],
    field: str,
    rank: dict[str, int],
) -> tuple[dict[str, Any], dict[str, Any]]:
    existing_rank = rank.get(str(existing.get(field)), 0)
    candidate_rank = rank.get(str(candidate.get(field)), 0)
    if candidate_rank > existing_rank:
        return candidate, existing
    return existing, candidate


def _choose_more_complete(
    existing: dict[str, Any],
    candidate: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    existing_score = sum(1 for value in existing.values() if _has_value(value))
    candidate_score = sum(1 for value in candidate.values() if _has_value(value))
    if candidate_score > existing_score:
        return candidate, existing
    return existing, candidate


def _normalize_text(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"[^\w\s]", "", text)
    return text.strip()


def _similar(a: str, b: str) -> bool:
    return SequenceMatcher(None, a, b).ratio() >= SIMILARITY_THRESHOLD


def _joined_list_text(value: Any) -> str:
    return _normalize_text(" ".join(str(item) for item in _list(value)))


def _rule_ids_overlap_or_missing(left: Any, right: Any) -> bool:
    left_ids = set(_ints(left))
    right_ids = set(_ints(right))
    return not left_ids or not right_ids or bool(left_ids & right_ids)


def _merge_ints(left: Any, right: Any) -> list[int]:
    return sorted(set(_ints(left)) | set(_ints(right)))


def _ints(value: Any) -> list[int]:
    result: list[int] = []
    for item in _list(value):
        try:
            result.append(int(item))
        except (TypeError, ValueError):
            continue
    return result


def _merge_strings(left: Any, right: Any) -> list[str]:
    return _dedupe_strings([str(item) for item in [*_list(left), *_list(right)] if str(item).strip()])


def _dedupe_strings(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        key = _normalize_text(value)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _int_or_large(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 10**9


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict)):
        return bool(value)
    return True
