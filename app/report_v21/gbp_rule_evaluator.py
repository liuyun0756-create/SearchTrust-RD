"""Deterministic semantic evaluation for GBP comparison rules 26-29.

L3 asks whether the checked page and GBP resolve to the same real-world
business entity.  Exact text equality is retained as a useful diagnostic, but
only a material semantic conflict triggers a scored L3 rule.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Callable

from app.report_v21.coverage import build_gbp_status
from app.report_v21.page_facts import normalize_phone
from app.report_v21.scoring import RULE_FINDING_LABELS
from app.report_v21.us_address_parser import parse_us_address


GBP_RULE_SPECS: dict[int, tuple[str, str, str]] = {
    26: ("business_name", "business_names", "name"),
    27: ("address", "addresses", "address"),
    28: ("phone", "phones", "phone"),
    29: ("service_area", "service_areas", "service_areas"),
}

_LEGAL_NAME_SUFFIXES = {
    "co", "company", "corp", "corporation", "inc", "incorporated", "llc",
    "llp", "lp", "ltd", "limited", "pllc", "pc",
}
_GENERIC_NAME_TOKENS = {
    "and", "at", "business", "company", "contractor", "local", "of", "provider",
    "service", "services", "the", "plumber", "plumbers", "plumbing",
}
_STREET_TOKEN_ALIASES = {
    "avenue": "ave", "ave": "ave",
    "boulevard": "blvd", "blvd": "blvd",
    "circle": "cir", "cir": "cir",
    "court": "ct", "ct": "ct",
    "drive": "dr", "dr": "dr",
    "highway": "hwy", "hwy": "hwy",
    "lane": "ln", "ln": "ln",
    "parkway": "pkwy", "pkwy": "pkwy",
    "place": "pl", "pl": "pl",
    "road": "rd", "rd": "rd",
    "street": "st", "st": "st",
    "trail": "trl", "trl": "trl",
    "north": "n", "n": "n",
    "south": "s", "s": "s",
    "east": "e", "e": "e",
    "west": "w", "w": "w",
}
_US_STATE_NAMES = {
    "alabama": "al", "alaska": "ak", "arizona": "az", "arkansas": "ar",
    "california": "ca", "colorado": "co", "connecticut": "ct", "delaware": "de",
    "florida": "fl", "georgia": "ga", "hawaii": "hi", "idaho": "id",
    "illinois": "il", "indiana": "in", "iowa": "ia", "kansas": "ks",
    "kentucky": "ky", "louisiana": "la", "maine": "me", "maryland": "md",
    "massachusetts": "ma", "michigan": "mi", "minnesota": "mn", "mississippi": "ms",
    "missouri": "mo", "montana": "mt", "nebraska": "ne", "nevada": "nv",
    "new hampshire": "nh", "new jersey": "nj", "new mexico": "nm", "new york": "ny",
    "north carolina": "nc", "north dakota": "nd", "ohio": "oh", "oklahoma": "ok",
    "oregon": "or", "pennsylvania": "pa", "rhode island": "ri",
    "south carolina": "sc", "south dakota": "sd", "tennessee": "tn", "texas": "tx",
    "utah": "ut", "vermont": "vt", "virginia": "va", "washington": "wa",
    "west virginia": "wv", "wisconsin": "wi", "wyoming": "wy",
    "district of columbia": "dc",
}
_US_STATE_CODES = frozenset(_US_STATE_NAMES.values())


def evaluate_gbp_rules(context: dict[str, Any]) -> tuple[dict[int, bool], dict[int, bool], dict[str, Any]]:
    """Return backend-owned semantic results, applicability and finding context."""
    page_facts = context.get("page_facts") if isinstance(context.get("page_facts"), dict) else {}
    gbp = context.get("gbp_data") if isinstance(context.get("gbp_data"), dict) else {}
    gbp_status = str(build_gbp_status(context).get("status") or "not_checked")
    gbp_checked = gbp_status == "checked"

    results: dict[int, bool] = {}
    applicability: dict[int, bool] = {}
    findings: dict[str, Any] = {}

    for rule_id, (field, page_key, gbp_key) in GBP_RULE_SPECS.items():
        page_observations = _page_observations(page_facts, page_key)
        page_values = [str(item["value"]) for item in page_observations]
        gbp_values = _values(gbp.get(gbp_key))
        normalized_page = _normalized_values_for_rule(rule_id, page_values, page_observations)
        normalized_gbp = _normalized_values_for_rule(rule_id, gbp_values, [])

        service_area_not_applicable = (
            rule_id == 29
            and bool(gbp.get("service_areas_observed"))
            and gbp.get("service_area_business") is False
        )

        if not gbp_checked:
            triggered = False
            field_applicable = False
            condition = "gbp_unavailable"
            match_type = "not_assessed"
            explanation = "A checked GBP reference was unavailable, so this comparison was not assessed."
            matched_pairs: list[dict[str, str]] = []
        elif service_area_not_applicable:
            triggered = False
            field_applicable = False
            condition = "field_not_applicable"
            match_type = "not_assessed"
            explanation = "The checked GBP identifies a storefront where service-area comparison is not applicable."
            matched_pairs = []
        elif not page_values and not gbp_values:
            triggered = False
            field_applicable = False
            condition = "both_missing"
            match_type = "not_assessed"
            explanation = "Neither checked source exposed this field; field presence is handled separately from L3."
            matched_pairs = []
        elif not page_values:
            triggered = False
            field_applicable = False
            condition = "page_missing"
            match_type = "not_assessed"
            explanation = "GBP exposed this field, but the page did not; page presence is handled by L2 rather than L3."
            matched_pairs = []
        elif not gbp_values:
            triggered = False
            field_applicable = False
            condition = "gbp_field_missing"
            match_type = "not_assessed"
            explanation = "The page exposed this field, but the checked GBP response did not return a comparable value."
            matched_pairs = []
        else:
            field_applicable = True
            condition, match_type, explanation, matched_pairs = _compare_rule(
                rule_id,
                page_values,
                gbp_values,
                page_observations,
                page_facts,
                gbp,
            )
            triggered = condition == "material_conflict"

        normalizer = _normalizer_for_rule(rule_id)
        results[rule_id] = triggered
        applicability[rule_id] = field_applicable
        findings[f"rule_{rule_id}"] = {
            "finding_key": f"rule_{rule_id}",
            "rule_id": rule_id,
            "affected_layer": "entity_consistency",
            "field": field,
            "triggered": triggered,
            "applicable": field_applicable,
            "condition": condition,
            "match_type": match_type,
            "comparator_version": "l3_semantic_v1",
            "finding": RULE_FINDING_LABELS[rule_id],
            "explanation": explanation,
            "page_values": page_values,
            "gbp_values": gbp_values,
            "normalized_page_values": normalized_page,
            "normalized_gbp_values": normalized_gbp,
            "matched_pairs": matched_pairs,
            "unmatched_page_values": _unmatched_values(page_values, matched_pairs, "page_value"),
            "unmatched_gbp_values": _unmatched_values(gbp_values, matched_pairs, "gbp_value"),
            "page_observations": page_observations,
            "gbp_observations": [
                {
                    "value": value,
                    "raw_value": value,
                    "normalized_value": normalizer(value),
                    "source": f"gbp.public.{gbp_key}",
                    "source_type": f"gbp.public.{gbp_key}",
                    "source_label": f"GBP · {field.replace('_', ' ').title()}",
                    "source_system": "gbp",
                    "scope": "gbp_profile",
                    "source_scope": "gbp_profile",
                    "validation": "valid",
                    "eligible_for_l3": field_applicable,
                }
                for value in gbp_values
            ],
            "gbp_status": gbp_status,
        }

    return results, applicability, findings


def _compare_rule(
    rule_id: int,
    page_values: list[str],
    gbp_values: list[str],
    page_observations: list[dict[str, Any]],
    page_facts: dict[str, Any],
    gbp: dict[str, Any],
) -> tuple[str, str, str, list[dict[str, str]]]:
    if rule_id == 26:
        return _compare_any_pair(
            page_values,
            gbp_values,
            lambda left, right: _business_names_equivalent(
                left,
                right,
                _name_location_tokens(page_facts, gbp),
            ),
            "The page and GBP business names identify the same core brand after safe formatting and legal-suffix normalization.",
            "The page and GBP business names do not share the same distinctive brand identity.",
        )
    if rule_id == 27:
        observation_by_value = {
            str(item.get("value") or ""): item
            for item in page_observations
            if isinstance(item, dict)
        }

        def address_match(page_value: str, gbp_value: str) -> str | None:
            return _addresses_equivalent(
                page_value,
                gbp_value,
                observation_by_value.get(page_value, {}),
            )

        return _compare_addresses(page_values, gbp_values, address_match)
    if rule_id == 28:
        return _compare_any_pair(
            page_values,
            gbp_values,
            lambda left, right: bool(_normalize_phone(left)) and _normalize_phone(left) == _normalize_phone(right),
            "At least one valid page phone number matches a checked GBP phone number.",
            "The page and GBP expose valid phone numbers, but none of the numbers match.",
        )
    return _compare_service_areas(page_values, gbp_values)


def _compare_any_pair(
    page_values: list[str],
    gbp_values: list[str],
    equivalent: Callable[[str, str], bool],
    semantic_explanation: str,
    conflict_explanation: str,
) -> tuple[str, str, str, list[dict[str, str]]]:
    matched_pairs = [
        {"page_value": page_value, "gbp_value": gbp_value}
        for page_value in page_values
        for gbp_value in gbp_values
        if equivalent(page_value, gbp_value)
    ]
    if not matched_pairs:
        return "material_conflict", "conflict", conflict_explanation, []
    exact = any(
        _exact_text(pair["page_value"]) == _exact_text(pair["gbp_value"])
        for pair in matched_pairs
    )
    if exact and len(page_values) == len(gbp_values) == 1:
        return "exact_match", "exact", "The checked page and GBP values are exactly equal.", matched_pairs
    return "semantic_match", "semantic", semantic_explanation, matched_pairs


def _compare_service_areas(
    page_values: list[str],
    gbp_values: list[str],
) -> tuple[str, str, str, list[dict[str, str]]]:
    page_by_key = {_normalize_area(value): value for value in page_values if _normalize_area(value)}
    gbp_by_key = {_normalize_area(value): value for value in gbp_values if _normalize_area(value)}
    page_keys = set(page_by_key)
    gbp_keys = set(gbp_by_key)
    shared = page_keys.intersection(gbp_keys)
    pairs = [
        {"page_value": page_by_key[key], "gbp_value": gbp_by_key[key]}
        for key in sorted(shared)
    ]
    if not shared:
        return (
            "material_conflict",
            "conflict",
            "The page and GBP expose service areas, but the normalized place sets do not overlap.",
            [],
        )
    if page_keys == gbp_keys and all(
        _exact_text(page_by_key[key]) == _exact_text(gbp_by_key[key]) for key in page_keys
    ):
        return "exact_match", "exact", "The checked page and GBP service-area sets are exactly equal.", pairs
    if page_keys.issubset(gbp_keys):
        return (
            "semantic_match",
            "semantic",
            "The page service area is a compatible subset of the checked GBP service area.",
            pairs,
        )
    if gbp_keys.issubset(page_keys):
        return (
            "compatible_difference",
            "compatible",
            "The checked GBP service area is contained within the broader page coverage statement.",
            pairs,
        )
    return (
        "compatible_difference",
        "compatible",
        "The page and GBP service areas overlap. Their additional place names are treated as compatible coverage differences.",
        pairs,
    )


def _compare_addresses(
    page_values: list[str],
    gbp_values: list[str],
    relationship: Callable[[str, str], str | None],
) -> tuple[str, str, str, list[dict[str, str]]]:
    relationships = [
        (page_value, gbp_value, relation)
        for page_value in page_values
        for gbp_value in gbp_values
        if (relation := relationship(page_value, gbp_value))
    ]
    if not relationships:
        return (
            "material_conflict",
            "conflict",
            "The page and GBP addresses contain a material location conflict.",
            [],
        )
    pairs = [
        {"page_value": page_value, "gbp_value": gbp_value}
        for page_value, gbp_value, _ in relationships
    ]
    if len(page_values) == len(gbp_values) == 1 and _exact_text(page_values[0]) == _exact_text(gbp_values[0]):
        return "exact_match", "exact", "The checked page and GBP values are exactly equal.", pairs
    if any(relation == "semantic" for _, _, relation in relationships):
        return (
            "semantic_match",
            "semantic",
            "The page and GBP addresses resolve to the same location after component and abbreviation normalization.",
            pairs,
        )
    return (
        "compatible_difference",
        "compatible",
        "The page and GBP addresses share the same core location, but one source omits an optional address component.",
        pairs,
    )


def _business_names_equivalent(
    left: str,
    right: str,
    allowed_qualifier_tokens: set[str] | None = None,
) -> bool:
    left_tokens = _name_core_tokens(left)
    right_tokens = _name_core_tokens(right)
    if not left_tokens or not right_tokens:
        return False
    if left_tokens == right_tokens:
        return True
    shorter, longer = sorted((left_tokens, right_tokens), key=len)
    extras = _subsequence_extras(shorter, longer)
    if extras is None:
        return False
    # Extra distinctive brand words are material. Only verified location
    # qualifiers may extend an otherwise identical core name.
    return bool(extras) and all(
        token in (allowed_qualifier_tokens or set())
        for token in extras
    )


def _name_core_tokens(value: str) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold().replace("&", " and ")
    tokens = re.findall(r"[a-z0-9]+", normalized)
    while tokens and tokens[-1] in _LEGAL_NAME_SUFFIXES:
        tokens.pop()
    return tuple(token for token in tokens if token not in _GENERIC_NAME_TOKENS)


def _addresses_equivalent(
    page_value: str,
    gbp_value: str,
    page_observation: dict[str, Any],
) -> str | None:
    if _normalize_address_text(page_value) == _normalize_address_text(gbp_value):
        return "semantic"
    page_components = (
        page_observation.get("components")
        if isinstance(page_observation.get("components"), dict)
        else {}
    )
    gbp_components = parse_us_address(gbp_value).components
    if not page_components:
        page_components = parse_us_address(page_value).components
    if not page_components or not gbp_components:
        return None

    page_house = _component_key(page_components.get("house_number"))
    gbp_house = _component_key(gbp_components.get("house_number"))
    page_street = _normalize_street(page_components.get("street"))
    gbp_street = _normalize_street(gbp_components.get("street"))
    if not page_house or not gbp_house or not page_street or not gbp_street:
        return None
    if page_house != gbp_house or page_street != gbp_street:
        return None

    comparisons = (
        (_normalize_place(page_components.get("city")), _normalize_place(gbp_components.get("city"))),
        (_normalize_state(page_components.get("state")), _normalize_state(gbp_components.get("state"))),
        (_normalize_postal(page_components.get("postal_code")), _normalize_postal(gbp_components.get("postal_code"))),
        (_normalize_place(page_components.get("country")), _normalize_place(gbp_components.get("country"))),
        (_normalize_unit(page_components.get("unit")), _normalize_unit(gbp_components.get("unit"))),
    )
    if any(left and right and left != right for left, right in comparisons):
        return None
    return "compatible" if any(bool(left) != bool(right) for left, right in comparisons) else "semantic"


def _normalize_address_text(value: Any) -> str:
    tokens = re.findall(r"[a-z0-9]+", unicodedata.normalize("NFKC", str(value or "")).casefold())
    return " ".join(_STREET_TOKEN_ALIASES.get(token, _US_STATE_NAMES.get(token, token)) for token in tokens)


def _normalize_street(value: Any) -> str:
    tokens = re.findall(r"[a-z0-9]+", unicodedata.normalize("NFKC", str(value or "")).casefold())
    return " ".join(_STREET_TOKEN_ALIASES.get(token, token) for token in tokens)


def _normalize_area(value: Any) -> str:
    normalized = _normalize_place(value)
    normalized = re.sub(r"^(?:city of|greater)\s+", "", normalized)
    normalized = re.sub(r"\s+(?:area|metro|region)$", "", normalized)
    parts = normalized.split()
    if len(parts) > 1 and parts[-1] in _US_STATE_CODES:
        parts.pop()
    return " ".join(parts)


def _normalize_place(value: Any) -> str:
    normalized = re.sub(
        r"[^a-z0-9]+",
        " ",
        unicodedata.normalize("NFKC", str(value or "")).casefold(),
    ).strip()
    return _US_STATE_NAMES.get(normalized, normalized)


def _normalize_state(value: Any) -> str:
    normalized = _normalize_place(value)
    return _US_STATE_NAMES.get(normalized, normalized)


def _normalize_postal(value: Any) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    return digits[:5]


def _normalize_unit(value: Any) -> str:
    normalized = _normalize_place(value)
    return re.sub(r"^(?:apartment|apt|suite|ste|unit)\s+", "", normalized)


def _component_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", _normalize_place(value))


def _subsequence_extras(
    shorter: tuple[str, ...],
    longer: tuple[str, ...],
) -> tuple[str, ...] | None:
    matched_indexes: list[int] = []
    cursor = 0
    for token in shorter:
        try:
            index = longer.index(token, cursor)
        except ValueError:
            return None
        matched_indexes.append(index)
        cursor = index + 1
    matched = set(matched_indexes)
    return tuple(token for index, token in enumerate(longer) if index not in matched)


def _name_location_tokens(page_facts: dict[str, Any], gbp: dict[str, Any]) -> set[str]:
    values = [
        *_values(page_facts.get("service_areas")),
        *_values(gbp.get("service_areas")),
    ]
    for raw_address in [*_values(page_facts.get("addresses")), *_values(gbp.get("address"))]:
        components = parse_us_address(raw_address).components
        values.extend([
            str(components.get("city") or ""),
            str(components.get("state") or ""),
        ])
    return {
        token
        for value in values
        for token in re.findall(r"[a-z0-9]+", _normalize_place(value))
        if token
    }


def _values(value: Any) -> list[str]:
    if isinstance(value, list):
        return [text for item in value if (text := str(item or "").strip())]
    text = str(value or "").strip()
    return [text] if text else []


def _normalized_values_for_rule(
    rule_id: int,
    values: list[str],
    observations: list[dict[str, Any]],
) -> list[str]:
    normalizer = _normalizer_for_rule(rule_id)
    normalized = [normalizer(value) for value in values]
    return list(dict.fromkeys(value for value in normalized if value))


def _normalizer_for_rule(rule_id: int) -> Callable[[str], str]:
    return {
        26: lambda value: " ".join(_name_core_tokens(value)),
        27: _normalize_address_text,
        28: _normalize_phone,
        29: _normalize_area,
    }[rule_id]


def _unmatched_values(
    values: list[str],
    matched_pairs: list[dict[str, str]],
    key: str,
) -> list[str]:
    matched = {pair[key] for pair in matched_pairs if pair.get(key)}
    return [value for value in values if value not in matched]


def _exact_text(value: Any) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(value or "")).strip())


def _page_observations(
    page_facts: dict[str, Any],
    page_key: str,
) -> list[dict[str, Any]]:
    """Return only validated target-page observations eligible for L3."""
    observations = page_facts.get("observations")
    raw_items = observations.get(page_key) if isinstance(observations, dict) else None
    if isinstance(raw_items, list):
        selected = [
            dict(item)
            for item in raw_items
            if (
                isinstance(item, dict)
                and str(item.get("value") or "").strip()
                and str(item.get("scope") or item.get("source_scope") or "target_page")
                in {"page", "target_page"}
                and item.get("eligible_for_l3") is not False
                and str(item.get("validation") or "valid") == "valid"
            )
        ]
        for item in selected:
            item["value"] = str(item.get("value") or "").strip()
        return selected
    if str(page_facts.get("version") or "") in {"3", "4", "5", "6"}:
        return []
    page_values = _values(page_facts.get(page_key))
    return [
        {
            "value": value,
            "raw_value": value,
            "source": "legacy.checked_page",
            "source_type": "legacy.checked_page",
            "scope": "target_page",
            "source_scope": "target_page",
            "validation": "valid",
            "eligible_for_l3": True,
        }
        for value in page_values
    ]


def _normalize_phone(value: str) -> str:
    normalized, _ = normalize_phone(value)
    return normalized
