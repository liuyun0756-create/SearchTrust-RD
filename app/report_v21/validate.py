"""Strong validation helpers for report_v2_1 payloads."""

from __future__ import annotations

import copy
import json
import re
from typing import Any

from pydantic import ValidationError

from app.report_v21.gbp_guard import BLOCKED_GBP_CLAIM_PHRASES, guard_gbp_claims
from app.report_v21.models import REQUIRED_LAYER_KEYS, ReportV21

OLD_LAYER_LABELS: tuple[str, ...] = (
    "Six-Layer Model",
    "L0-RELEVANCE",
    "L1-ENTITY CLARITY",
    "L2-PROOF SIGNALS",
    "L3-LOCAL FIT",
    "L4-STRUTURAL TRUST",
    "L5-STANDALONE VALUE",
    "six-layer trust diagnosis",
    "six layer trust model",
)

GOOGLE_CERTAINTY_PHRASES: tuple[str, ...] = (
    "Google cannot determine",
    "Google cannot confirm",
    "Google cannot verify",
    "Google has confirmed",
    "Google knows",
    "Google sees",
    "Google will rank",
    "Google will not rank",
)

_PARAGRAPH_RE = re.compile(r"\S(?:.*\S)?", re.DOTALL)


def validate_report_v21(report: Any) -> dict[str, Any]:
    """Validate a report and return errors, warnings, and optional sanitized copy."""
    errors: list[str] = []
    warnings: list[str] = []
    inner = _inner_report(report)

    if not isinstance(inner, dict):
        return {
            "valid": False,
            "errors": ["report_v2_1 must be an object"],
            "warnings": warnings,
            "sanitized_report": None,
        }

    original_inner = copy.deepcopy(inner)
    guarded_inner, guard_warnings = guard_gbp_claims(inner)
    warnings.extend(_dedupe(guard_warnings))
    sanitized_report = guarded_inner if guarded_inner != original_inner else None
    inner = guarded_inner

    _validate_contract(inner, errors)
    _validate_labels_and_certainty(inner, errors)
    _validate_gbp(inner, errors, warnings)
    _validate_data_coverage(inner, errors, warnings)
    _validate_duplicates(inner, warnings)
    _validate_primary_blocker(inner, errors, warnings)

    return {
        "valid": not errors,
        "errors": _dedupe(errors),
        "warnings": _dedupe(warnings),
        "sanitized_report": sanitized_report,
    }


def _inner_report(report: Any) -> Any:
    if isinstance(report, dict) and isinstance(report.get("report_v2_1"), dict):
        return report["report_v2_1"]
    return report


def _validate_contract(report: dict[str, Any], errors: list[str]) -> None:
    if report.get("schema_version") != "2.1":
        errors.append('schema_version must be "2.1"')

    layers = report.get("layers")
    if not isinstance(layers, list):
        errors.append("layers must be an array")
    elif len(layers) != 8:
        errors.append("layers must contain exactly 8 items")
    else:
        actual_keys = [layer.get("layer_key") if isinstance(layer, dict) else None for layer in layers]
        expected_keys = list(REQUIRED_LAYER_KEYS)
        if actual_keys != expected_keys:
            errors.append(f"layers must use required order: {expected_keys}")

    try:
        ReportV21.model_validate(report)
    except ValidationError as exc:
        errors.append(f"Pydantic contract validation failed: {exc.errors()}")


def _validate_labels_and_certainty(report: dict[str, Any], errors: list[str]) -> None:
    serialized = json.dumps(report, ensure_ascii=False, default=str)
    serialized_lower = serialized.lower()

    for label in OLD_LAYER_LABELS:
        if label.lower() in serialized_lower:
            errors.append(f"old six-layer label is not allowed: {label}")

    for phrase in GOOGLE_CERTAINTY_PHRASES:
        if phrase.lower() in serialized_lower:
            errors.append(f"unsupported Google certainty claim is not allowed: {phrase}")


def _validate_gbp(report: dict[str, Any], errors: list[str], warnings: list[str]) -> None:
    gbp_status = report.get("gbp_status")
    gbp_checked = isinstance(gbp_status, dict) and gbp_status.get("status") == "checked"
    serialized = json.dumps(report, ensure_ascii=False, default=str)
    serialized_lower = serialized.lower()

    if not gbp_checked:
        for phrase in BLOCKED_GBP_CLAIM_PHRASES:
            if phrase.lower() in serialized_lower:
                errors.append(f"unsupported GBP claim is not allowed when GBP is not checked: {phrase}")
        if _contains_source_type(report, "gbp"):
            errors.append('source_type="gbp" is not allowed when GBP is not checked')
        return

    has_gbp_comparison_language = any(
        phrase.lower() in serialized_lower for phrase in BLOCKED_GBP_CLAIM_PHRASES
    )
    if has_gbp_comparison_language and not _contains_source_type(report, "gbp"):
        warnings.append("GBP comparison language appears, but no GBP evidence item exists in the report.")


def _validate_data_coverage(report: dict[str, Any], errors: list[str], warnings: list[str]) -> None:
    gbp_status = report.get("gbp_status")
    coverage = report.get("data_coverage")
    if not isinstance(gbp_status, dict):
        errors.append("gbp_status is required")
        return
    if not isinstance(coverage, dict):
        errors.append("data_coverage is required")
        return

    gbp_checked = gbp_status.get("status") == "checked"
    if coverage.get("gbp_checked") != gbp_checked:
        errors.append("data_coverage.gbp_checked must match gbp_status.status == checked")

    if coverage.get("reviews_checked") is True and not gbp_checked:
        errors.append("data_coverage.reviews_checked cannot be true when GBP is not checked")

    if coverage.get("competitor_pages_checked") is not False:
        errors.append("data_coverage.competitor_pages_checked must be false for v2.1")

    if _contains_source_type(report, "review") and coverage.get("reviews_checked") is not True:
        warnings.append("Review evidence exists, but data_coverage.reviews_checked is false.")

    limitations = coverage.get("limitations")
    if not gbp_checked and not (isinstance(limitations, list) and len(limitations) > 0):
        errors.append("data_coverage.limitations must be non-empty when GBP is not checked")


def _validate_duplicates(report: dict[str, Any], warnings: list[str]) -> None:
    key_issues = report.get("key_issues")
    if isinstance(key_issues, list):
        titles = [
            str(issue.get("issue_title", "")).strip().lower()
            for issue in key_issues
            if isinstance(issue, dict)
        ]
        _warn_duplicates(titles, "duplicate key issue title", warnings)

    optimization = report.get("optimization_path")
    if isinstance(optimization, dict):
        for section in ("must_execute_now", "defer_until_later", "do_not_prioritize_yet"):
            items = optimization.get(section)
            if isinstance(items, list):
                titles = [
                    str(item.get("task_title", "")).strip().lower()
                    for item in items
                    if isinstance(item, dict)
                ]
                _warn_duplicates(titles, f"duplicate action task_title in {section}", warnings)

        roadmap = optimization.get("roadmap")
        if isinstance(roadmap, list):
            phase_titles = [
                str(phase.get("phase_title", "")).strip().lower()
                for phase in roadmap
                if isinstance(phase, dict)
            ]
            _warn_duplicates(phase_titles, "duplicate roadmap phase title", warnings)

    paragraphs = _collect_long_strings(report)
    _warn_duplicates(paragraphs, "repeated exact paragraph-like string longer than 120 characters", warnings)


def _validate_primary_blocker(
    report: dict[str, Any],
    errors: list[str],
    warnings: list[str],
) -> None:
    blocker = report.get("primary_blocking_layer")
    if not isinstance(blocker, dict):
        return
    layer_key = blocker.get("layer_key")
    if layer_key not in REQUIRED_LAYER_KEYS:
        errors.append("primary_blocking_layer.layer_key is not one of the required layer keys")
        return

    layers = report.get("layers")
    if not isinstance(layers, list):
        return
    for layer in layers:
        if isinstance(layer, dict) and layer.get("layer_key") == layer_key:
            if layer.get("status") == "good":
                warnings.append("primary_blocking_layer references a layer whose status is good")
            return


def _contains_source_type(value: Any, source_type: str) -> bool:
    if isinstance(value, dict):
        if value.get("source_type") == source_type:
            return True
        return any(_contains_source_type(item, source_type) for item in value.values())
    if isinstance(value, list):
        return any(_contains_source_type(item, source_type) for item in value)
    return False


def _collect_long_strings(value: Any) -> list[str]:
    strings: list[str] = []
    if isinstance(value, str):
        text = value.strip()
        if len(text) > 120 and _PARAGRAPH_RE.match(text):
            strings.append(text)
    elif isinstance(value, list):
        for item in value:
            strings.extend(_collect_long_strings(item))
    elif isinstance(value, dict):
        for item in value.values():
            strings.extend(_collect_long_strings(item))
    return strings


def _warn_duplicates(values: list[str], label: str, warnings: list[str]) -> None:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        if not value:
            continue
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    for value in sorted(duplicates):
        warnings.append(f"{label}: {value}")


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result
