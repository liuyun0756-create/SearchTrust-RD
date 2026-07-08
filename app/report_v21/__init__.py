"""SearchTrust report_v2_1 backend support."""

from __future__ import annotations

from typing import Any

__all__ = [
    "build_data_coverage",
    "build_gbp_status",
    "apply_deterministic_scoring",
    "calculate_layer_status",
    "dedupe_report_v21",
    "guard_gbp_claims",
    "extract_report_v21_from_outputs",
    "legacy_score_to_report_v21",
    "normalize_report_to_v21",
    "parse_json_maybe",
    "validate_report_v21",
]


def __getattr__(name: str) -> Any:
    """Lazy exports keep pure helpers importable without loading Pydantic modules."""
    if name in {"build_data_coverage", "build_gbp_status"}:
        from app.report_v21.coverage import build_data_coverage, build_gbp_status

        return {
            "build_data_coverage": build_data_coverage,
            "build_gbp_status": build_gbp_status,
        }[name]
    if name == "guard_gbp_claims":
        from app.report_v21.gbp_guard import guard_gbp_claims

        return guard_gbp_claims
    if name == "dedupe_report_v21":
        from app.report_v21.dedupe import dedupe_report_v21

        return dedupe_report_v21
    if name in {"apply_deterministic_scoring", "calculate_layer_status"}:
        from app.report_v21.scoring import apply_deterministic_scoring, calculate_layer_status

        return {
            "apply_deterministic_scoring": apply_deterministic_scoring,
            "calculate_layer_status": calculate_layer_status,
        }[name]
    if name in {
        "extract_report_v21_from_outputs",
        "legacy_score_to_report_v21",
        "normalize_report_to_v21",
        "parse_json_maybe",
    }:
        from app.report_v21.normalize import (
            extract_report_v21_from_outputs,
            legacy_score_to_report_v21,
            normalize_report_to_v21,
            parse_json_maybe,
        )

        return {
            "extract_report_v21_from_outputs": extract_report_v21_from_outputs,
            "legacy_score_to_report_v21": legacy_score_to_report_v21,
            "normalize_report_to_v21": normalize_report_to_v21,
            "parse_json_maybe": parse_json_maybe,
        }[name]
    if name == "validate_report_v21":
        from app.report_v21.validate import validate_report_v21

        return validate_report_v21
    raise AttributeError(name)
