"""Deterministic GBP claim guard for report_v2_1."""

from __future__ import annotations

import copy
import re
from typing import Any

SAFE_GBP_UNVERIFIED_TEXT = (
    "GBP was not checked in this report, so GBP alignment could not be verified."
)

GBP_EVIDENCE_UNVERIFIED_TEXT = (
    "GBP was not checked in this report, so GBP evidence could not be verified."
)

ENTITY_CONSISTENCY_ACTION_TEXT = (
    "Review the page, footer, contact page, and available business identity "
    "signals for consistency."
)

BLOCKED_GBP_CLAIM_PHRASES: tuple[str, ...] = (
    "compared to Google Business Profile",
    "compared to GBP",
    "Google Business Profile mismatch",
    "GBP mismatch",
    "address differs from GBP",
    "address differs from Google Business Profile",
    "service area differs from GBP",
    "service area does not match GBP",
    "synchronize with Google Business Profile",
    "align with Google Business Profile",
    "stronger association with the Google Business Profile",
    "Google Business Profile shows",
    "GBP shows",
    "matches the Google Business Profile",
    "exact Google Business Profile",
    "does not clearly align with the supplied GBP data",
    "does not clearly align with supplied GBP data",
    "does not align with the supplied GBP data",
    "does not align with supplied GBP data",
    "does not match the supplied GBP data",
    "does not match supplied GBP data",
    "entity match assessment",
)

_BLOCKED_RE = re.compile(
    "|".join(re.escape(phrase) for phrase in BLOCKED_GBP_CLAIM_PHRASES),
    re.IGNORECASE,
)


def guard_gbp_claims(report_v2_1: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Return a sanitized report copy plus guard warnings."""
    report = copy.deepcopy(report_v2_1)
    warnings: list[str] = []

    gbp_status = report.get("gbp_status", {})
    status = gbp_status.get("status") if isinstance(gbp_status, dict) else None

    if status == "checked":
        return report, warnings

    guarded = _sanitize_value(report, warnings)
    if warnings and isinstance(guarded, dict):
        coverage = guarded.get("data_coverage")
        if isinstance(coverage, dict):
            limitations = coverage.setdefault("limitations", [])
            if isinstance(limitations, list):
                for warning in warnings:
                    if warning not in limitations:
                        limitations.append(warning)
    return guarded, warnings


def contains_blocked_gbp_claim(value: Any) -> bool:
    """Return True when a string or nested structure contains a blocked GBP claim."""
    if isinstance(value, str):
        return bool(_BLOCKED_RE.search(value))
    if isinstance(value, list):
        return any(contains_blocked_gbp_claim(item) for item in value)
    if isinstance(value, dict):
        return any(contains_blocked_gbp_claim(item) for item in value.values())
    return False


def _sanitize_value(value: Any, warnings: list[str]) -> Any:
    if isinstance(value, str):
        return _sanitize_text(value, warnings)
    if isinstance(value, list):
        return [_sanitize_value(item, warnings) for item in value]
    if isinstance(value, dict):
        return _sanitize_dict(value, warnings)
    return value


def _sanitize_dict(value: dict[str, Any], warnings: list[str]) -> dict[str, Any]:
    if value.get("source_type") == "gbp":
        warnings.append("GBP evidence was sanitized because GBP was not checked.")
        sanitized = dict(value)
        sanitized["source_type"] = "not_available"
        sanitized["comparison_result"] = "not_checked"
        sanitized["confidence"] = "low"
        sanitized["explanation"] = GBP_EVIDENCE_UNVERIFIED_TEXT
        sanitized.pop("source_url", None)
        return {key: _sanitize_value(item, warnings) for key, item in sanitized.items()}

    if _looks_like_action_item(value) and contains_blocked_gbp_claim(value):
        warnings.append("GBP action item was rewritten because GBP was not checked.")
        rewritten = dict(value)
        rewritten["task_title"] = "Review business identity consistency"
        rewritten["affected_layer"] = "entity_consistency"
        rewritten["where_to_add"] = ["Page footer", "Contact page", "Business identity sections"]
        rewritten["what_to_add"] = [ENTITY_CONSISTENCY_ACTION_TEXT]
        rewritten["example_copy"] = []
        rewritten["implementation_notes"] = [
            "Do not recommend GBP synchronization until GBP has been checked.",
            ENTITY_CONSISTENCY_ACTION_TEXT,
        ]
        rewritten["completion_signals"] = [
            "Business name, phone, address, and service-area statements are internally consistent across checked site pages."
        ]
        rewritten["expected_effect"] = (
            "Improves entity consistency based on site-controlled business identity signals."
        )
        return {key: _sanitize_value(item, warnings) for key, item in rewritten.items()}

    return {key: _sanitize_value(item, warnings) for key, item in value.items()}


def _sanitize_text(value: str, warnings: list[str]) -> str:
    if not _BLOCKED_RE.search(value):
        return value
    warnings.append("Unsupported GBP comparison text was rewritten because GBP was not checked.")
    return SAFE_GBP_UNVERIFIED_TEXT


def _looks_like_action_item(value: dict[str, Any]) -> bool:
    return (
        "task_title" in value
        or "where_to_add" in value
        or "what_to_add" in value
        or "implementation_notes" in value
        or "completion_signals" in value
        or "expected_effect" in value
    )
