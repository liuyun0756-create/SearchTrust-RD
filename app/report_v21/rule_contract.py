"""Authoritative rule-result contract shared by Dify validation and scoring."""

from __future__ import annotations

import json
import re
from typing import Any

# Rule 5 was retired before v2.1. The product retains the historical 1-39
# numbering, so the active contract contains these 38 stable rule identifiers.
ACTIVE_RULE_IDS: tuple[int, ...] = tuple(rule_id for rule_id in range(1, 40) if rule_id != 5)
ACTIVE_RULE_KEYS: tuple[str, ...] = tuple(f"rule_{rule_id}" for rule_id in ACTIVE_RULE_IDS)
GBP_COMPARISON_RULE_IDS: frozenset[int] = frozenset({26, 27, 28, 29})
ENTITY_PRESENCE_RULE_IDS: frozenset[int] = frozenset({21, 22, 23, 24, 25})

V21_RULE_RESULTS_INVALID_CODE = "V21_RULE_RESULTS_INVALID"
V21_LANGUAGE_INVALID_CODE = "V21_LANGUAGE_INVALID"


class RetryableDifyOutputError(RuntimeError):
    """A structurally invalid workflow result that requires a full retry."""

    retryable = True

    def __init__(self, error_code: str, message: str, details: list[str] | None = None):
        super().__init__(message)
        self.error_code = error_code
        self.details = details or []


def parse_rule_results(
    outputs: Any,
    *,
    backend_presence_results: dict[int, bool] | None = None,
    backend_presence_applicability: dict[int, bool] | None = None,
    backend_gbp_results: dict[int, bool] | None = None,
    backend_gbp_applicability: dict[int, bool] | None = None,
) -> tuple[dict[int, bool], dict[int, bool]]:
    """Parse Dify's vector and override backend-owned deterministic rules."""
    if not isinstance(outputs, dict):
        raise RetryableDifyOutputError(
            V21_RULE_RESULTS_INVALID_CODE,
            "Dify output did not include a rule-results object.",
        )

    raw_results = _parse_json_object(outputs.get("rule_results"), "rule_results")
    raw_applicability = _parse_json_object(
        outputs.get("rule_applicability"),
        "rule_applicability",
    )
    raw_errors = outputs.get("rule_errors")
    if isinstance(raw_errors, str):
        try:
            raw_errors = json.loads(raw_errors)
        except json.JSONDecodeError:
            raw_errors = [raw_errors]
    backend_owns_gbp = backend_gbp_results is not None or backend_gbp_applicability is not None
    backend_owns_presence = (
        backend_presence_results is not None
        or backend_presence_applicability is not None
    )
    backend_owned_rule_ids = set()
    if backend_owns_presence:
        backend_owned_rule_ids.update(ENTITY_PRESENCE_RULE_IDS)
    if backend_owns_gbp:
        backend_owned_rule_ids.update(GBP_COMPARISON_RULE_IDS)
    expected = {
        key
        for key in ACTIVE_RULE_KEYS
        if int(key.removeprefix("rule_")) not in backend_owned_rule_ids
    }
    allowed = set(ACTIVE_RULE_KEYS)
    result_keys = set(raw_results)
    applicability_keys = set(raw_applicability)
    errors: list[str] = []

    if isinstance(raw_errors, list):
        for item in raw_errors:
            message = str(item).strip()
            if not message:
                continue
            if backend_owned_rule_ids and any(
                f"rule_{rule_id}" in message for rule_id in backend_owned_rule_ids
            ):
                continue
            errors.append(message)
    elif raw_errors not in (None, "", []):
        errors.append("rule_errors must be an array when provided.")

    if not expected.issubset(result_keys) or not result_keys.issubset(allowed):
        errors.append(_key_difference("rule_results", expected, result_keys))
    if not expected.issubset(applicability_keys) or not applicability_keys.issubset(allowed):
        errors.append(_key_difference("rule_applicability", expected, applicability_keys))

    for key in expected & result_keys:
        if type(raw_results[key]) is not bool:  # bool only; reject 0/1 and strings
            errors.append(f"{key} must be a boolean in rule_results.")
    for key in expected & applicability_keys:
        if type(raw_applicability[key]) is not bool:
            errors.append(f"{key} must be a boolean in rule_applicability.")

    if backend_owns_presence:
        _validate_backend_rule_vector(
            "Entity Presence",
            ENTITY_PRESENCE_RULE_IDS,
            backend_presence_results,
            backend_presence_applicability,
            errors,
        )

    if backend_owns_gbp:
        _validate_backend_rule_vector(
            "GBP",
            GBP_COMPARISON_RULE_IDS,
            backend_gbp_results,
            backend_gbp_applicability,
            errors,
        )
    else:
        for rule_id in GBP_COMPARISON_RULE_IDS:
            key = f"rule_{rule_id}"
            if raw_applicability.get(key) is False and raw_results.get(key) is True:
                errors.append(f"{key} cannot trigger when the GBP comparison is not applicable.")

    if errors:
        raise RetryableDifyOutputError(
            V21_RULE_RESULTS_INVALID_CODE,
            "Dify returned an incomplete or invalid rule-results vector.",
            errors,
        )

    results = {
        rule_id: (
            (backend_presence_results or {})[rule_id]
            if backend_owns_presence and rule_id in ENTITY_PRESENCE_RULE_IDS
            else
            (backend_gbp_results or {})[rule_id]
            if backend_owns_gbp and rule_id in GBP_COMPARISON_RULE_IDS
            else raw_results[f"rule_{rule_id}"]
        )
        for rule_id in ACTIVE_RULE_IDS
    }
    applicability = {
        rule_id: (
            (backend_presence_applicability or {})[rule_id]
            if backend_owns_presence and rule_id in ENTITY_PRESENCE_RULE_IDS
            else
            (backend_gbp_applicability or {})[rule_id]
            if backend_owns_gbp and rule_id in GBP_COMPARISON_RULE_IDS
            else raw_applicability[f"rule_{rule_id}"]
        )
        for rule_id in ACTIVE_RULE_IDS
    }
    return results, applicability


def _validate_backend_rule_vector(
    label: str,
    expected_rule_ids: frozenset[int],
    results: dict[int, bool] | None,
    applicability: dict[int, bool] | None,
    errors: list[str],
) -> None:
    expected_label = ", ".join(str(rule_id) for rule_id in sorted(expected_rule_ids))
    if set(results or {}) != set(expected_rule_ids):
        errors.append(
            f"Backend {label} rule results must contain exactly rules {expected_label}."
        )
    if set(applicability or {}) != set(expected_rule_ids):
        errors.append(
            f"Backend {label} rule applicability must contain exactly rules {expected_label}."
        )
    for rule_id in expected_rule_ids:
        if type((results or {}).get(rule_id)) is not bool:
            errors.append(f"Backend rule_{rule_id} result must be boolean.")
        if type((applicability or {}).get(rule_id)) is not bool:
            errors.append(f"Backend rule_{rule_id} applicability must be boolean.")


def validate_english_narrative(outputs: Any) -> None:
    """Reject Chinese characters in Dify-owned narrative fields."""
    report = _extract_native_report(outputs)
    narrative_values: list[tuple[str, str]] = []

    def collect(value: Any, path: str) -> None:
        if isinstance(value, str):
            narrative_values.append((path, value))
        elif isinstance(value, list):
            for index, item in enumerate(value):
                collect(item, f"{path}[{index}]")
        elif isinstance(value, dict):
            for key, item in value.items():
                if key == "evidence_items":
                    continue
                collect(item, f"{path}.{key}" if path else key)

    for key in (
        "overall_status",
        "ranking_potential",
        "risk_level",
        "primary_blocking_layer",
        "page_level",
        "layers",
        "key_issues",
        "optimization_path",
        "client_summary",
    ):
        collect(report.get(key), key)

    failures = [path for path, value in narrative_values if re.search(r"[\u3400-\u4dbf\u4e00-\u9fff]", value)]
    if failures:
        raise RetryableDifyOutputError(
            V21_LANGUAGE_INVALID_CODE,
            "Dify returned non-English report narrative.",
            failures[:20],
        )


def _extract_native_report(outputs: Any) -> dict[str, Any]:
    if not isinstance(outputs, dict):
        raise RetryableDifyOutputError(
            V21_LANGUAGE_INVALID_CODE,
            "Dify output was not an object.",
        )
    value = outputs.get("report_v2_1")
    if isinstance(value, str):
        cleaned = re.sub(r"^```(?:json)?\s*", "", value.strip(), flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned).strip()
        try:
            value = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            raise RetryableDifyOutputError(
                V21_LANGUAGE_INVALID_CODE,
                "Dify report_v2_1 was not valid JSON.",
                [str(exc)],
            ) from exc
    if isinstance(value, dict) and isinstance(value.get("report_v2_1"), dict):
        value = value["report_v2_1"]
    if not isinstance(value, dict):
        raise RetryableDifyOutputError(
            V21_LANGUAGE_INVALID_CODE,
            "Dify output did not include a native report_v2_1 object.",
        )
    return value


def _parse_json_object(value: Any, field: str) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise RetryableDifyOutputError(
                V21_RULE_RESULTS_INVALID_CODE,
                f"{field} was not valid JSON.",
                [str(exc)],
            ) from exc
        if isinstance(parsed, dict):
            return parsed
    raise RetryableDifyOutputError(
        V21_RULE_RESULTS_INVALID_CODE,
        f"{field} must be a JSON object.",
    )


def _key_difference(label: str, expected: set[str], actual: set[str]) -> str:
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    return f"{label} keys mismatch; missing={missing}, extra={extra}."
