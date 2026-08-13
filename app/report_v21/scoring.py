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

RULE_FINDING_LABELS: dict[int, str] = {
    1: "City references can be swapped without changing the page meaning",
    2: "Service descriptions remain highly generic",
    3: "No non-administrative geographic or entity anchor is present",
    4: "No meaningful time or activity trace is present",
    6: "Visual content appears reusable or template-based",
    7: "The page lacks first-person action and concrete work detail",
    8: "Calls to action remain generic",
    9: "The page serves search demand without showing operational responsibility",
    10: "Real-world constraints and complex cases are not addressed",
    11: "No externally verifiable trust clue is present",
    12: "Accountability for real-world outcomes is unclear",
    13: "The page belongs to a highly similar page cluster",
    14: "The page lacks a clear reason to be indexed separately",
    15: "The page pattern relies on legacy authority more than current trust signals",
    16: "The page exists mainly to cover search demand",
    17: "The business name narrows how the entity can be interpreted",
    18: "The claimed service exceeds the entity's primary category",
    19: "Entity identity varies across the site",
    20: "The page targets a query space where entity qualification is unclear",
    21: "An identifiable real business entity is not clearly present",
    22: "A physical address was not found",
    23: "A contact phone number was not found",
    24: "The service area is not clearly declared",
    25: "Business hours were not found",
    26: "The business name materially conflicts with the checked GBP record",
    27: "The address materially conflicts with the checked GBP record",
    28: "No valid page phone number matches the checked GBP record",
    29: "The service area materially conflicts with the checked GBP record",
    30: "Community-level geographic detail was not found",
    31: "A concrete landmark reference was not found",
    32: "Local context language was not found",
    33: "A service radius or operational boundary is not defined",
    34: "A specific service case was not found",
    35: "Customer context is not described",
    36: "Meaningful time context is not present",
    37: "Reviews lack specific service detail",
    38: "Reviews lack geographic context",
    39: "Review service topics do not align with the page focus",
}

GOOD_LAYER_NARRATIVES: dict[str, tuple[str, str]] = {
    "foundation": (
        "The page has a sound qualification foundation.",
        "The assessed foundation signals establish a clear page purpose and a usable basis for the later trust layers.",
    ),
    "entity_presence": (
        "The core business entity is visibly present.",
        "The assessed identity and contact signals give users a clear business presence to connect with the page.",
    ),
    "entity_consistency": (
        "The assessed entity signals are consistent overall.",
        "The checked identity fields support a stable connection between the page and the business record.",
    ),
    "specificity": (
        "The page is sufficiently specific overall.",
        "Most assessed details make the page meaningfully tied to its service purpose instead of reading as a generic template.",
    ),
    "real_world_connection": (
        "The page shows a credible real-world connection.",
        "The assessed local and operational signals connect the page to genuine service activity and place context.",
    ),
    "accountability": (
        "The page communicates service accountability well.",
        "The assessed responsibility and delivery signals help users understand who acts, what happens, and how the business stands behind the service.",
    ),
    "page_unique_value": (
        "The page provides distinct standalone value.",
        "The assessed content gives the page a useful purpose and value that extends beyond a reusable search landing page.",
    ),
    "algorithm_fit": (
        "The page aligns well with the assessed trust signals.",
        "The current structure and proof provide a sound basis for search systems to interpret the page's purpose and credibility.",
    ),
}

LAYER_THRESHOLDS: dict[str, tuple[range, range, range]] = {
    "foundation": (range(0, 2), range(2, 3), range(3, 5)),
    "entity_presence": (range(0, 2), range(2, 4), range(4, 6)),
    # L3 findings are now material semantic conflicts rather than formatting
    # differences, so a single finding is meaningful and two are weak.
    "entity_consistency": (range(0, 1), range(1, 2), range(2, 5)),
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
        layer["triggered_findings"] = [
            RULE_FINDING_LABELS[rule_id]
            for rule_id in layer["triggered_rule_ids"]
            if rule_id in RULE_FINDING_LABELS
        ]
        if len(layer["triggered_rule_ids"]) != len(incoming_triggered):
            warnings.append(f"Ignored triggered rule ids outside {layer_key}'s fixed assessment scope.")
        layer["status"] = calculate_layer_status(layer_key, layer["triggered_rule_ids"])
        if layer["status"] == "good" and not layer["triggered_rule_ids"]:
            layer["presentation_mode"] = "healthy"
            layer["suggested_fixes"] = []
            layer["summary"], layer["explanation"] = GOOD_LAYER_NARRATIVES[layer_key]
        elif layer["status"] == "good":
            layer["presentation_mode"] = "healthy_with_opportunities"
            layer["summary"], positive_explanation = GOOD_LAYER_NARRATIVES[layer_key]
            count = len(layer["triggered_rule_ids"])
            noun = "opportunity" if count == 1 else "opportunities"
            layer["explanation"] = (
                f"{positive_explanation} "
                f"The {count} confirmed {noun} below can strengthen this layer further."
            )
        else:
            layer["presentation_mode"] = "attention"

    return report, _dedupe_strings(warnings), all_available


def calculate_overall_status(layers: list[dict[str, Any]]) -> dict[str, str]:
    status_counts = _status_counts(layers)
    weak_count = status_counts["weak"]
    medium_count = status_counts["medium"]
    assessed_count = sum(status_counts.values())

    if weak_count >= 4:
        level, label = "weak", "Weak"
    elif weak_count >= 1 or medium_count >= 3:
        level, label = "medium_weak", "Medium Weak"
    elif medium_count >= 1 or assessed_count < len(REQUIRED_LAYER_KEYS):
        level, label = "medium", "Medium"
    else:
        level, label = "strong", "Good"

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
        level, label = "low", "Low Potential"
    elif counts["good"] >= 3:
        level, label = "strong", "Strong Competitive Potential"
    elif counts["good"] >= 2:
        level, label = "improvable", "Improvement Potential"
    elif counts["medium"] >= 3:
        level, label = "competitive", "Competitive"
    else:
        level, label = "competitive", "Competitive"

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

    description = _page_level_description(label)
    strengths = _layer_names_by_status(statuses, {"good"})
    limitations = [
        *_layer_names_by_status(statuses, {"weak"}),
        *_layer_names_by_status(statuses, {"medium"}),
    ]
    return {
        "label": label,
        "what_it_looks_like": description,
        "strengths": strengths,
        "missing_elements": limitations,
        "current_assessment": description,
        "existing_foundation": _page_foundation_summary(strengths),
        "main_limitation": _page_limitation_summary(limitations),
        "likely_search_outcome": _page_search_outcome(label),
        "competitive_interpretation": _page_competitive_interpretation(label),
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
    incoming_page_level = scored_report.get("page_level")
    page_level = calculate_page_level(layers)
    if isinstance(incoming_page_level, dict):
        for field in (
            "current_assessment",
            "existing_foundation",
            "main_limitation",
            "likely_search_outcome",
            "competitive_interpretation",
        ):
            value = incoming_page_level.get(field)
            if isinstance(value, str) and value.strip():
                page_level[field] = value.strip()
    scored_report["page_level"] = page_level
    client_summary = scored_report.get("client_summary")
    if isinstance(client_summary, dict):
        client_summary["decision_context"] = build_client_decision_context(scored_report)
    return scored_report, _dedupe_strings(warnings)


def build_client_decision_context(report_v2_1: dict[str, Any]) -> dict[str, Any]:
    """Build non-scoring client decision context from the final report."""
    issues = [
        issue
        for issue in report_v2_1.get("key_issues", [])
        if isinstance(issue, dict)
    ]
    affected_keys = {
        str(issue.get("affected_layer"))
        for issue in issues
        if str(issue.get("affected_layer")) in REQUIRED_LAYER_KEYS
    }
    ordered_keys = [key for key in REQUIRED_LAYER_KEYS if key in affected_keys]
    earliest_key = ordered_keys[0] if ordered_keys else None
    earliest_number = REQUIRED_LAYER_KEYS.index(earliest_key) + 1 if earliest_key else None

    if earliest_number is None:
        priority_level = "monitor"
        priority_label = "Monitor and maintain"
        why_act_now = (
            "No confirmed Key Issues were produced. Maintain the current trust signals "
            "and re-audit after meaningful page or business changes."
        )
    elif earliest_number <= 3:
        priority_level = "immediate"
        priority_label = "Immediate foundation repair"
        why_act_now = (
            f"Confirmed issues begin at {LAYER_DISPLAY_LABELS[earliest_key]}. Lower-numbered "
            "layers support the work above them, so later optimization can deliver weaker "
            "returns until this foundation is repaired."
        )
    elif earliest_number <= 5:
        priority_level = "high"
        priority_label = "High-priority trust repair"
        why_act_now = (
            f"Confirmed issues begin at {LAYER_DISPLAY_LABELS[earliest_key]}. Addressing this "
            "page-level trust gap now makes later proof and differentiation work more credible."
        )
    else:
        priority_level = "planned"
        priority_label = "Planned trust strengthening"
        why_act_now = (
            f"Core foundations are holding, but the confirmed gaps begin at "
            f"{LAYER_DISPLAY_LABELS[earliest_key]} and still limit how distinctive and "
            "defensible the page can become."
        )

    work_sequence = _client_work_sequence(ordered_keys)
    overall = _display_value(report_v2_1.get("overall_status"))
    ranking = _display_value(report_v2_1.get("ranking_potential"))
    risk = _display_value(report_v2_1.get("risk_level"))

    return {
        "priority_level": priority_level,
        "priority_label": priority_label,
        "why_act_now": why_act_now,
        "issue_count": len(issues),
        "affected_layer_count": len(ordered_keys),
        "work_phase_count": len(work_sequence),
        "score_interpretation": (
            f"Trust Status ({overall}) describes current page strength. Ranking Potential "
            f"({ranking}) describes the upside available after repair. Risk Level ({risk}) "
            "measures the coverage of severe weak layers; it does not mean confirmed issues "
            "can be ignored."
        ),
        "work_sequence": work_sequence,
    }


def _client_work_sequence(ordered_keys: list[str]) -> list[dict[str, Any]]:
    if not ordered_keys:
        return []

    earliest_key = ordered_keys[0]
    earliest_index = REQUIRED_LAYER_KEYS.index(earliest_key)
    build_next = [
        key
        for key in ordered_keys[1:]
        if REQUIRED_LAYER_KEYS.index(key) <= 4
    ]
    strengthen_after = [
        key
        for key in ordered_keys
        if REQUIRED_LAYER_KEYS.index(key) >= 5 and key != earliest_key
    ]
    phases: list[dict[str, Any]] = [
        _client_phase(
            "fix_first",
            "Fix first",
            [earliest_key],
            f"Resolve {LAYER_DISPLAY_LABELS[earliest_key]} before investing in later-layer work.",
        )
    ]
    if build_next:
        phases.append(_client_phase(
            "build_next",
            "Build next",
            build_next,
            "Build the next page-level trust foundations after the first blocker is resolved.",
        ))
    if strengthen_after:
        phases.append(_client_phase(
            "strengthen_after",
            "Strengthen after",
            strengthen_after,
            "Strengthen accountability, differentiation, and current search-era fit.",
        ))

    # L6-L8 can be the first affected layer. It remains the first approved phase
    # instead of appearing twice in the later-strengthening group.
    if earliest_index >= 5 and len(phases) == 1:
        phases[0]["summary"] = (
            f"Strengthen {LAYER_DISPLAY_LABELS[earliest_key]} as the first confirmed opportunity."
        )
    return phases


def _client_phase(
    stage: str,
    label: str,
    layer_keys: list[str],
    summary: str,
) -> dict[str, Any]:
    return {
        "stage": stage,
        "label": label,
        "layer_keys": layer_keys,
        "layer_labels": [LAYER_DISPLAY_LABELS[key] for key in layer_keys],
        "summary": summary,
    }


def _display_value(value: Any) -> str:
    if not isinstance(value, dict):
        return "Not available"
    return str(value.get("label") or value.get("level") or "Not available")


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
    explanations = {
        "weak": "The page has significant structural trust gaps. Core foundations should be repaired before advanced optimization.",
        "medium_weak": "The page has some local relevance, but its trust structure is still incomplete and ranking stability remains weak.",
        "medium": "The page has workable foundations, but several important trust layers still need strengthening before performance can become stable.",
        "strong": "The page has a complete trust foundation and a solid base for local competition.",
    }
    return explanations[level]


def _ranking_explanation(level: str) -> str:
    explanations = {
        "strong": "The page has a solid foundation and, with further optimization, can move into a stronger competitive tier.",
        "improvable": "The page has an existing competitive foundation. Strengthening the remaining key layers can improve ranking potential.",
        "competitive": "The page can participate in local competition, but its main trust gaps should be repaired before expecting more stable performance.",
        "low": "The competitive foundation is still insufficient, making stable local performance difficult in the near term.",
    }
    return explanations[level]


def _risk_explanation(level: str, weak_count: int) -> str:
    explanations = {
        "high": "The page has major structural trust gaps that can materially limit visibility and stability.",
        "medium_high": "The page has visible trust gaps that increase the likelihood of unstable performance until repaired.",
        "medium": "The page has some weaknesses, but it still retains room for repair and optimization.",
        "low": "The page does not show obvious structural trust weaknesses across the key risk layers.",
    }
    return explanations[level]


def _page_level_description(label: str) -> str:
    descriptions = {
        "Low": "The page is missing core trust foundations and should be stabilized before advanced optimization.",
        "Medium Weak": "The page has some foundation, but important specificity or real-world trust signals remain weak.",
        "Medium": "The page has workable foundations with remaining trust gaps that limit stronger performance.",
        "Medium Strong": "The page is mostly strong, with only one limited layer holding it back.",
        "High": "The page has strong trust signals across all eight layers.",
    }
    return descriptions[label]


def _page_foundation_summary(strengths: list[str]) -> str:
    if not strengths:
        return "No trust layer is currently strong enough to serve as a dependable foundation."
    return f"The strongest current foundations are {', '.join(strengths)}."


def _page_limitation_summary(limitations: list[str]) -> str:
    if not limitations:
        return "No material layer limitation was identified by the current assessment."
    return f"The main limitations are concentrated in {', '.join(limitations)}."


def _page_search_outcome(label: str) -> str:
    outcomes = {
        "Low": "Stable visibility is unlikely until the core trust foundations are repaired.",
        "Medium Weak": "The page may gain limited visibility, but performance is likely to remain unstable in stronger competition.",
        "Medium": "The page can participate in relevant search results, but unresolved trust gaps may limit consistency.",
        "Medium Strong": "The page is positioned to compete, with one remaining limitation preventing stronger stability.",
        "High": "The page has the trust structure needed to compete more consistently, subject to normal market conditions.",
    }
    return outcomes[label]


def _page_competitive_interpretation(label: str) -> str:
    interpretations = {
        "Low": "Competitors with clearer entity and real-world signals are likely to hold an advantage.",
        "Medium Weak": "The page may compete in lower-pressure results but is vulnerable to pages with stronger local proof.",
        "Medium": "The page has optimization value, but stronger competitors can still win through more complete trust signals.",
        "Medium Strong": "The page is close to a strong competitive position and needs a focused final improvement.",
        "High": "The page has a strong trust foundation for sustained local competition.",
    }
    return interpretations[label]


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
