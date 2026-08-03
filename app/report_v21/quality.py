"""Evidence quality gates for native v2.1 reports.

The workflow owns the narrative, but a native report is only usable when its
material conclusions can be traced back to an observed source.  These checks
are intentionally stricter than presentation validation: a failed gate is a
retryable workflow-output failure, never a reason to show a confident report.
"""

from __future__ import annotations

import json
import re
from typing import Any


DIRECT_SOURCES = {
    "page",
    "gbp",
    "schema",
    "contact_page",
    "about_page",
    "review",
    "site_internal",
}


def validate_evidence_quality(report: dict[str, Any], context: dict[str, Any]) -> list[str]:
    """Return actionable errors for unsupported material conclusions."""
    errors: list[str] = []
    context = {**context, "gbp_status": report.get("gbp_status")}
    layers = _records(report.get("layers"))
    layers_by_key = {
        str(layer.get("layer_key")): layer
        for layer in layers
        if isinstance(layer.get("layer_key"), str)
    }

    for layer in layers:
        status = layer.get("status")
        if status not in {"weak", "medium"}:
            continue
        key = str(layer.get("layer_key") or "unknown layer")
        evidence = _records(layer.get("evidence_items"))
        if not _has_direct_evidence(evidence):
            errors.append(f"{key}: {status} layer requires at least one direct evidence item.")
        if not _integers(layer.get("triggered_rule_ids")):
            errors.append(f"{key}: {status} layer requires at least one triggered assessment signal.")
        errors.extend(_validate_traceability(evidence, context, f"{key} layer"))

    for issue in _records(report.get("key_issues")):
        severity = issue.get("severity")
        if severity not in {"high", "medium"}:
            continue
        title = str(issue.get("issue_title") or issue.get("id") or "key issue")
        layer = layers_by_key.get(str(issue.get("affected_layer")))
        evidence = _records(issue.get("evidence_items")) or _records(layer.get("evidence_items") if layer else None)
        if not _has_direct_evidence(evidence):
            errors.append(f"{title}: {severity} key issue requires directly traceable evidence.")
        if not _records(issue.get("recommended_actions")):
            errors.append(f"{title}: {severity} key issue requires at least one executable action.")
        errors.extend(_validate_traceability(evidence, context, f"{title} key issue"))

    return _dedupe(errors)


def prune_unsupported_evidence(report: dict[str, Any], context: dict[str, Any]) -> list[str]:
    """Remove evidence cards that cannot be traced to an audited source.

    Evidence is presentation support, not the authoritative rule decision.
    Invalid or unavailable evidence therefore degrades to an empty evidence
    section instead of invalidating the full report.
    """
    warnings: list[str] = []
    scoped_context = {**context, "gbp_status": report.get("gbp_status")}

    def clean(owner: str, value: Any) -> list[dict[str, Any]]:
        kept: list[dict[str, Any]] = []
        for item in _records(value):
            item_errors = _validate_traceability([item], scoped_context, owner)
            if not _has_direct_evidence([item]) or item_errors:
                warnings.extend(item_errors)
                if not item_errors:
                    warnings.append(f"{owner}: unsupported evidence was omitted from the report.")
                continue
            kept.append(item)
        return kept

    layers = _records(report.get("layers"))
    for layer in layers:
        key = str(layer.get("layer_key") or "unknown layer")
        layer["evidence_items"] = clean(
            f"{key} layer",
            layer.get("evidence_items"),
        )

    for issue in _records(report.get("key_issues")):
        owner = str(issue.get("issue_title") or issue.get("id") or "key issue")
        issue["evidence_items"] = clean(
            f"{owner} key issue",
            issue.get("evidence_items"),
        )

    blocker = _record(report.get("primary_blocking_layer"))
    if blocker:
        blocker["evidence_items"] = clean(
            "primary blocking layer",
            blocker.get("evidence_items"),
        )

    return _dedupe(warnings)


def _has_direct_evidence(items: list[dict[str, Any]]) -> bool:
    return any(
        item.get("source_type") in DIRECT_SOURCES
        and item.get("comparison_result") != "not_checked"
        and _text(item.get("source_label"))
        and _text(item.get("explanation"))
        and (_text(item.get("page_section")) or _text(item.get("source_url")))
        and (_text(item.get("extracted_text")) or _text(item.get("normalized_value")))
        for item in items
    )


def _validate_traceability(
    items: list[dict[str, Any]],
    context: dict[str, Any],
    owner: str,
) -> list[str]:
    errors: list[str] = []
    content = _normalized(context.get("content"))
    gbp_payload = _normalized(json.dumps(context.get("gbp_data") or {}, ensure_ascii=False))
    review_payload = _normalized(json.dumps(context.get("review_corpus") or [], ensure_ascii=False))
    gbp_checked = _text(_record(context.get("gbp_status")).get("status")) == "checked"

    for item in items:
        source_type = item.get("source_type")
        extracted = _normalized(item.get("extracted_text"))
        normalized_value = _normalized(item.get("normalized_value"))
        label = _text(item.get("source_label")) or "evidence item"

        if source_type not in DIRECT_SOURCES:
            continue
        if not (extracted or normalized_value):
            errors.append(f"{owner}: {label} must include an original excerpt or normalized value.")
            continue
        if source_type in {"page", "contact_page", "about_page", "site_internal"} and extracted and content:
            if extracted not in content:
                errors.append(f"{owner}: page excerpt for {label} was not found in scraped content.")
        if source_type == "gbp":
            if not gbp_checked:
                errors.append(f"{owner}: GBP evidence cannot support a conclusion until GBP is checked.")
            elif (extracted or normalized_value) and gbp_payload and not (
                (extracted and extracted in gbp_payload)
                or (normalized_value and normalized_value in gbp_payload)
            ):
                errors.append(f"{owner}: GBP value for {label} was not found in backend GBP data.")
        if source_type == "review" and extracted and review_payload and extracted not in review_payload:
            errors.append(f"{owner}: review excerpt for {label} was not found in the task review corpus.")
    return errors


def _records(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _record(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _integers(value: Any) -> list[int]:
    return [item for item in value if isinstance(item, int)] if isinstance(value, list) else []


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _normalized(value: Any) -> str:
    return re.sub(r"\s+", " ", _text(value)).strip().casefold()


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))
