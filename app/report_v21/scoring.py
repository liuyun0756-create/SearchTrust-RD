"""Deterministic scoring helpers for report_v2_1 payloads."""

from __future__ import annotations

import copy
from typing import Any

from app.report_v21.models import LAYER_DISPLAY_LABELS


REQUIRED_LAYER_KEYS: tuple[str, ...] = (
    "foundation",
    "entity_presence",
    "entity_consistency",
    "specificity",
    "real_world_connection",
    "accountability",
    "page_unique_value",
    "algorithm_fit",
)

LAYER_LABELS: dict[str, str] = {
    "foundation": "Foundation",
    "entity_presence": "Entity Presence",
    "entity_consistency": "Entity Consistency",
    "specificity": "Specificity",
    "real_world_connection": "Real-World Connection",
    "accountability": "Accountability",
    "page_unique_value": "Page Unique Value",
    "algorithm_fit": "Algorithm Fit",
}

LAYER_RULES: dict[str, list[int]] = {
    "foundation": [17, 18, 19, 20],
    "entity_presence": [21, 22, 23, 24, 25],
    "entity_consistency": [26, 27, 28, 29],
    "specificity": [1, 2, 4, 6, 7, 8, 32, 34, 35, 36],
    "real_world_connection": [3, 11, 30, 31, 33],
    "accountability": [9, 10, 12],
    "page_unique_value": [13, 14, 16],
    "algorithm_fit": [15, 37, 38, 39],
}

LAYER_THRESHOLDS: dict[str, tuple[range, range, range]] = {
    "foundation": (range(0, 2), range(2, 3), range(3, 5)),
    "entity_presence": (range(0, 2), range(2, 4), range(4, 6)),
    "entity_consistency": (range(0, 2), range(2, 3), range(3, 5)),
    "specificity": (range(0, 4), range(4, 8), range(8, 11)),
    "real_world_connection": (range(0, 2), range(2, 4), range(4, 6)),
    "accountability": (range(0, 2), range(2, 3), range(3, 4)),
    "page_unique_value": (range(0, 2), range(2, 3), range(3, 4)),
    "algorithm_fit": (range(0, 2), range(2, 3), range(3, 5)),
}


def calculate_layer_status(layer_key: str, triggered_rule_ids: list[int]) -> str:
    """Calculate a layer status from triggered rule ids."""
    layer_rules = set(LAYER_RULES.get(layer_key, []))
    triggered_count = len({rule_id for rule_id in _int_list(triggered_rule_ids) if rule_id in layer_rules})
    thresholds = LAYER_THRESHOLDS.get(layer_key)
    if thresholds is None:
        return "not_checked"

    good_range, medium_range, weak_range = thresholds
    if triggered_count in good_range:
        return "good"
    if triggered_count in medium_range:
        return "medium"
    if triggered_count in weak_range:
        return "weak"
    return "weak"


def calculate_all_layer_statuses(report_v2_1: dict[str, Any]) -> tuple[dict[str, Any], list[str], bool]:
    """Apply the fixed eight-layer assessment contract to a report."""
    report = copy.deepcopy(report_v2_1)
    warnings: list[str] = []
    all_available = True
    layers = report.get("layers")

    if not isinstance(layers, list):
        return report, ["Layer status kept from model output because layers were unavailable."], False

    by_key = {layer.get("layer_key"): layer for layer in layers if isinstance(layer, dict)}
    for layer_key in REQUIRED_LAYER_KEYS:
        layer = by_key.get(layer_key)
        if not isinstance(layer, dict):
            all_available = False
            warnings.append(f"Layer status kept from model output because {layer_key} was unavailable.")
            continue

        triggered = layer.get("triggered_rule_ids")
        if not isinstance(triggered, list):
            all_available = False
            warnings.append("Layer status kept from model output because triggered_rule_ids were unavailable.")
            continue

        incoming_triggered = _int_list(triggered)
        allowed_rule_ids = set(LAYER_RULES[layer_key])
        layer["layer_id"] = REQUIRED_LAYER_KEYS.index(layer_key) + 1
        layer["layer_name"] = LAYER_LABELS[layer_key]
        layer["layer_label"] = LAYER_DISPLAY_LABELS[layer_key]
        # Assessment coverage is fixed by the backend.  Dify's checked ids are
        # not a reliable coverage counter and must not affect the UI or scoring.
        layer["checked_rule_ids"] = list(LAYER_RULES[layer_key])
        layer["triggered_rule_ids"] = [rule_id for rule_id in incoming_triggered if rule_id in allowed_rule_ids]
        if len(layer["triggered_rule_ids"]) != len(incoming_triggered):
            warnings.append(f"Ignored triggered rule ids outside {layer_key}'s fixed assessment scope.")
        layer["status"] = calculate_layer_status(layer_key, layer["triggered_rule_ids"])

    return report, _dedupe_strings(warnings), all_available


def calculate_overall_status(layers: list[dict[str, Any]]) -> dict[str, str]:
    status_counts = _status_counts(layers)
    weak_count = status_counts["weak"]
    good_count = status_counts["good"]

    if weak_count >= 4:
        level, label = "weak", "Weak"
    elif weak_count in {2, 3}:
        level, label = "medium_weak", "Medium Weak"
    elif weak_count <= 1 and good_count <= 2:
        level, label = "medium", "Medium"
    elif good_count >= 3 and weak_count == 0:
        level, label = "strong", "Strong"
    else:
        level, label = "medium_weak", "Medium Weak"

    return {
        "label": label,
        "level": level,
        "explanation": _overall_explanation(level, status_counts),
    }


def calculate_ranking_potential(layers: list[dict[str, Any]]) -> dict[str, str]:
    statuses = _status_by_layer(layers)
    key_layers = ("foundation", "entity_presence", "page_unique_value", "algorithm_fit")
    counts = _count_statuses(statuses, key_layers)

    # Severe gaps take precedence so a small number of good layers cannot hide
    # a structurally weak competitive foundation.
    if counts["weak"] >= 3:
        level, label = "low", "Low potential"
    elif counts["good"] >= 3:
        level, label = "strong", "Strong competitive potential"
    elif counts["good"] >= 2:
        level, label = "improvable", "Room to improve"
    elif counts["medium"] >= 3:
        level, label = "competitive", "Able to compete"
    else:
        level, label = "competitive", "Able to compete"

    return {
        "label": label,
        "level": level,
        "explanation": _ranking_explanation(level),
    }


def calculate_risk_level(layers: list[dict[str, Any]]) -> dict[str, str]:
    statuses = _status_by_layer(layers)
    risk_layers = ("entity_consistency", "real_world_connection", "accountability", "algorithm_fit")
    weak_count = sum(1 for key in risk_layers if statuses.get(key) == "weak")

    if weak_count >= 3:
        level, label = "high", "High"
    elif weak_count == 2:
        level, label = "medium_high", "Medium High"
    elif weak_count == 1:
        level, label = "medium", "Medium"
    else:
        level, label = "low", "Low"

    return {
        "label": label,
        "level": level,
        "explanation": _risk_explanation(level, weak_count),
    }


def calculate_page_level(layers: list[dict[str, Any]]) -> dict[str, Any]:
    statuses = _status_by_layer(layers)
    core_layers = ("foundation", "entity_presence", "entity_consistency")
    mid_layers = ("specificity", "real_world_connection")
    support_layers = ("specificity", "real_world_connection", "accountability", "page_unique_value")
    core_medium_count = sum(1 for key in core_layers if statuses.get(key) == "medium")

    if (
        statuses.get("foundation") == "weak"
        or statuses.get("entity_presence") == "weak"
        or statuses.get("entity_consistency") == "weak"
        or (
            sum(1 for key in ("entity_presence", "entity_consistency") if statuses.get(key) == "medium") >= 2
            and any(statuses.get(key) == "weak" for key in mid_layers)
        )
    ):
        label = "Low"
    elif (
        statuses.get("specificity") == "weak"
        and statuses.get("real_world_connection") == "weak"
        or core_medium_count >= 2
        or statuses.get("specificity") == "weak"
        or statuses.get("real_world_connection") == "weak"
        or all(statuses.get(key) == "medium" for key in mid_layers)
    ):
        label = "Medium Weak"
    elif (
        any(statuses.get(key) == "medium" for key in core_layers)
        and any(statuses.get(key) != "good" for key in support_layers)
        or statuses.get("accountability") in {"weak", "medium"}
        or statuses.get("page_unique_value") in {"weak", "medium"}
    ):
        label = "Medium"
    elif (
        sum(1 for key in core_layers if statuses.get(key) == "medium") == 1
        and sum(1 for key in REQUIRED_LAYER_KEYS if statuses.get(key) == "good") == 7
        or statuses.get("algorithm_fit") in {"medium", "weak"}
        and all(statuses.get(key) == "good" for key in REQUIRED_LAYER_KEYS if key != "algorithm_fit")
    ):
        label = "Medium Strong"
    elif all(statuses.get(key) == "good" for key in REQUIRED_LAYER_KEYS):
        label = "High"
    else:
        label = "Medium"

    return {
        "label": label,
        "what_it_looks_like": _page_level_description(label),
        "strengths": _layer_names_by_status(statuses, {"good"}),
        "missing_elements": [
            *_layer_names_by_status(statuses, {"weak"}),
            *_layer_names_by_status(statuses, {"medium"}),
        ],
    }


def apply_deterministic_scoring(report_v2_1: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Apply backend-owned scoring and presentation fields to a complete report."""
    scored_report, warnings, all_available = calculate_all_layer_statuses(report_v2_1)
    if not all_available:
        warnings.append("Deterministic aggregate scoring skipped because one or more layers were unavailable.")
        return scored_report, _dedupe_strings(warnings)

    layers = scored_report.get("layers")
    if not isinstance(layers, list) or len(layers) != 8:
        warnings.append("Deterministic aggregate scoring skipped because layers were incomplete.")
        return scored_report, _dedupe_strings(warnings)

    scored_report["overall_status"] = calculate_overall_status(layers)
    scored_report["ranking_potential"] = calculate_ranking_potential(layers)
    scored_report["risk_level"] = calculate_risk_level(layers)
    scored_report["page_level"] = calculate_page_level(layers)
    return scored_report, _dedupe_strings(warnings)


def _status_by_layer(layers: list[dict[str, Any]]) -> dict[str, str]:
    return {
        str(layer.get("layer_key")): str(layer.get("status"))
        for layer in layers
        if isinstance(layer, dict)
    }


def _status_counts(layers: list[dict[str, Any]]) -> dict[str, int]:
    statuses = _status_by_layer(layers).values()
    return {
        "good": sum(1 for status in statuses if status == "good"),
        "medium": sum(1 for status in statuses if status == "medium"),
        "weak": sum(1 for status in statuses if status == "weak"),
    }


def _count_statuses(statuses: dict[str, str], keys: tuple[str, ...]) -> dict[str, int]:
    return {
        "good": sum(1 for key in keys if statuses.get(key) == "good"),
        "medium": sum(1 for key in keys if statuses.get(key) == "medium"),
        "weak": sum(1 for key in keys if statuses.get(key) == "weak"),
    }


def _layer_names_by_status(statuses: dict[str, str], target_statuses: set[str]) -> list[str]:
    return [
        LAYER_LABELS[key]
        for key in REQUIRED_LAYER_KEYS
        if statuses.get(key) in target_statuses
    ]


def _overall_explanation(level: str, counts: dict[str, int]) -> str:
    return (
        f"Deterministic scoring classified the page as {level.replace('_', ' ')} "
        f"from {counts['good']} good, {counts['medium']} medium, and {counts['weak']} weak layers."
    )


def _ranking_explanation(level: str) -> str:
    explanations = {
        "strong": "Key competitive layers are mostly good, indicating strong competitive potential.",
        "improvable": "Some key competitive layers are good, but additional layer work can improve potential.",
        "competitive": "Key competitive layers are mostly medium, so the page can compete with improvements.",
        "low": "Key competitive layers are mostly weak, limiting stable competitive potential.",
    }
    return explanations[level]


def _risk_explanation(level: str, weak_count: int) -> str:
    return (
        f"Deterministic scoring found {weak_count} weak risk layers, "
        f"resulting in a {level.replace('_', ' ')} risk level."
    )


def _page_level_description(label: str) -> str:
    descriptions = {
        "Low": "The page is missing core trust foundations and should be stabilized before advanced optimization.",
        "Medium Weak": "The page has some foundation, but important specificity or real-world trust signals remain weak.",
        "Medium": "The page has workable foundations with remaining trust gaps that limit stronger performance.",
        "Medium Strong": "The page is mostly strong, with only one limited layer holding it back.",
        "High": "The page has strong trust signals across all eight layers.",
    }
    return descriptions[label]


def _int_list(values: Any) -> list[int]:
    result: list[int] = []
    if not isinstance(values, list):
        return result
    for value in values:
        try:
            result.append(int(value))
        except (TypeError, ValueError):
            continue
    return sorted(set(result))


def _dedupe_strings(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result
