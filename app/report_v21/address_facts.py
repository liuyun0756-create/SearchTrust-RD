"""Deterministic acceptance of source-aware page address candidates."""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Iterable

from app.report_v21.address_candidates import AddressCandidate
from app.report_v21.us_address_parser import (
    ParsedAddress,
    US_STATE_CODES,
    normalize_us_state,
    parse_us_address,
    parser_available,
)


_SOURCE_STRENGTH = {
    "page.jsonld.postal_address": 100,
    "page.microdata.postal_address": 98,
    "page.dom.address_element": 95,
    "page.dom.labeled_address_block": 92,
    "page.dom.map_address": 90,
    "page.ai.confirmed_address": 88,
    "page.dom.visible_address": 60,
    "page.dom.map_url_only": 0,
}
_STRUCTURED_SOURCE_TYPES = frozenset({
    "page.jsonld.postal_address",
    "page.microdata.postal_address",
})
_NON_US_ALLOWED_SOURCE_TYPES = frozenset({
    "page.jsonld.postal_address",
    "page.microdata.postal_address",
    "page.dom.address_element",
    "page.dom.labeled_address_block",
})
_STREET_FALLBACK_RE = re.compile(
    r"^(?P<number>\d{1,6})\s+(?P<street>.+?\s+"
    r"(?:Street|St\.?|Avenue|Ave\.?|Road|Rd\.?|Boulevard|Blvd\.?|Drive|Dr\.?|"
    r"Lane|Ln\.?|Court|Ct\.?|Highway|Hwy\.?|Way|Place|Pl\.?|Parkway|Pkwy\.?))"
    r"(?:\s+(?P<unit>(?:Suite|Ste\.?|Unit|#)\s*[A-Za-z0-9-]+))?$",
    re.IGNORECASE,
)
_US_COUNTRY_NAMES = frozenset({"", "us", "usa", "united states", "united states of america"})
_EXPLICIT_UNIT_RE = re.compile(
    r"^(?:(?:apt|apartment|bldg|building|dept|department|fl|floor|lot|rm|room|"
    r"ste|suite|unit)\b|#)",
    re.IGNORECASE,
)


def build_address_facts(candidates: list[AddressCandidate]) -> dict[str, Any]:
    processed = [_evaluate_candidate(candidate) for candidate in candidates]
    valid = [item for item in processed if item.get("validation") == "valid"]
    rejected = [item for item in processed if item.get("validation") != "valid"]
    accepted = _select_distinct_raw_values(valid)
    return {
        "schema_version": "1",
        "parser": "usaddress",
        "parser_available": parser_available(),
        "addresses": _unique(item["raw_value"] for item in accepted),
        "observations": accepted,
        "candidate_observations": processed,
        "rejected_observations": rejected,
    }


def _evaluate_candidate(candidate: AddressCandidate) -> dict[str, Any]:
    base = _base_observation(candidate)
    if candidate.map_url_only:
        return _reject(base, "hidden_map_url_only")

    parsed = _parsed_candidate(candidate)
    components = dict(parsed.components)
    country = _country_code(candidate.country_code or components.get("country", ""))
    state = str(components.get("state") or "").upper()
    if state in US_STATE_CODES and country != "US":
        # A valid US state followed by a parser-labelled "country" is almost
        # always the next footer/navigation line leaking into the candidate.
        # Do not reinterpret that trailing text as a foreign country.
        return _reject(
            base,
            "unexpected_trailing_content",
            components=components,
            parser_status=parsed.status,
        )
    if country and country != "US":
        return _evaluate_non_us(candidate, base, parsed, country)

    unit = str(components.get("unit") or "").strip()
    if unit and not _EXPLICIT_UNIT_RE.match(unit):
        # usaddress can classify arbitrary text after an otherwise complete
        # address as OccupancyIdentifier.  Only an explicitly labelled unit is
        # a real address component; footer labels, phone numbers and adjacent
        # navigation must remain outside the address fact.
        return _reject(
            base,
            "unexpected_trailing_content",
            components=components,
            parser_status=parsed.status,
        )

    reason = _us_rejection_reason(parsed)
    if reason:
        return _reject(base, reason, components=components, parser_status=parsed.status)

    group_key = _address_group_key(components, country_code="US")
    return {
        **base,
        "components": components,
        "country_code": "US",
        "address_group_key": group_key,
        "parser": parsed.parser,
        "parser_status": parsed.status,
        "address_type": parsed.address_type,
        "validation": "valid",
        "completeness": "full" if components.get("postal_code") else "postal_partial",
        "eligible_for_l2": True,
        "eligible_for_l3": True,
        "rejection_reason": None,
    }


def _parsed_candidate(candidate: AddressCandidate) -> ParsedAddress:
    if candidate.structured_components:
        structured = _parse_structured_components(candidate)
        if structured.status == "parsed" or not parser_available():
            return structured
    return parse_us_address(candidate.raw_value)


def _parse_structured_components(candidate: AddressCandidate) -> ParsedAddress:
    source = candidate.structured_components
    street_address = _clean(source.get("street_address"))
    parsed_street = _STREET_FALLBACK_RE.fullmatch(street_address)
    components = {
        "city": _clean(source.get("city")),
        "state": normalize_us_state(_clean(source.get("state"))),
        "postal_code": _clean(source.get("postal_code")),
        "country": _clean(source.get("country")),
    }
    if parsed_street:
        components.update({
            "house_number": _clean(parsed_street.group("number")),
            "street": _clean(parsed_street.group("street")),
            "street_name": _street_name(parsed_street.group("street")),
            "street_suffix": _street_suffix(parsed_street.group("street")),
            "unit": _clean(parsed_street.group("unit")),
        })
    else:
        parsed = parse_us_address(candidate.raw_value)
        components.update(parsed.components)
        return ParsedAddress(
            candidate.raw_value,
            parsed.status,
            parsed.address_type,
            {key: value for key, value in components.items() if value},
            parsed.rejection_reason,
            parsed.parser,
        )
    return ParsedAddress(
        candidate.raw_value,
        "parsed",
        "Street Address",
        {key: value for key, value in components.items() if value},
        parser="structured_components",
    )


def _us_rejection_reason(parsed: ParsedAddress) -> str | None:
    if parsed.status == "unavailable":
        return "parser_unavailable"
    if parsed.status == "ambiguous":
        return "ambiguous_parse"
    if parsed.status != "parsed" or parsed.address_type != "Street Address":
        return "not_street_address"
    components = parsed.components
    if components.get("unsupported"):
        return "not_street_address"
    if not components.get("house_number"):
        return "missing_house_number"
    if not components.get("street_name") or not components.get("street_suffix"):
        return "missing_street"
    if not components.get("city"):
        return "missing_city"
    state = str(components.get("state") or "").upper()
    if not state:
        return "missing_state"
    if state not in US_STATE_CODES:
        return "invalid_state"
    return None


def _evaluate_non_us(
    candidate: AddressCandidate,
    base: dict[str, Any],
    parsed: ParsedAddress,
    country: str,
) -> dict[str, Any]:
    if candidate.source_type not in _NON_US_ALLOWED_SOURCE_TYPES:
        return _reject(
            base,
            "unsupported_non_us_free_text",
            components=parsed.components,
            parser_status=parsed.status,
        )
    components = parsed.components
    street = components.get("street") or _clean(candidate.structured_components.get("street_address"))
    city = components.get("city") or _clean(candidate.structured_components.get("city"))
    if not street:
        return _reject(base, "missing_street", components=components, parser_status=parsed.status)
    if not city:
        return _reject(base, "missing_city", components=components, parser_status=parsed.status)
    group_components = {**components, "street": street, "city": city}
    return {
        **base,
        "components": group_components,
        "country_code": country,
        "address_group_key": _address_group_key(group_components, country_code=country),
        "parser": parsed.parser,
        "parser_status": parsed.status,
        "address_type": parsed.address_type,
        "validation": "valid",
        "completeness": "structured_non_us",
        "eligible_for_l2": True,
        "eligible_for_l3": True,
        "rejection_reason": None,
    }


def _base_observation(candidate: AddressCandidate) -> dict[str, Any]:
    raw = _clean(candidate.raw_value)
    return {
        "value": raw,
        "raw_value": raw,
        "normalized_value": _normalize(raw),
        "source": candidate.source_type,
        "source_type": candidate.source_type,
        "source_label": candidate.source_label,
        "source_system": "page",
        "scope": "target_page",
        "source_scope": "target_page",
        "source_url": candidate.source_url or None,
        "locator": candidate.locator or None,
        "excerpt": _clean(candidate.excerpt)[:500] or None,
        "visibility": candidate.visibility,
    }


def _reject(
    base: dict[str, Any],
    reason: str,
    *,
    components: dict[str, str] | None = None,
    parser_status: str = "not_run",
) -> dict[str, Any]:
    return {
        **base,
        "components": components or {},
        "parser": "usaddress",
        "parser_status": parser_status,
        "validation": "rejected",
        "completeness": "invalid",
        "eligible_for_l2": False,
        "eligible_for_l3": False,
        "rejection_reason": reason,
    }


def _select_distinct_raw_values(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_raw: dict[str, list[dict[str, Any]]] = {}
    for item in values:
        by_raw.setdefault(_normalize(item.get("raw_value")), []).append(item)

    selected: list[dict[str, Any]] = []
    for items in by_raw.values():
        items.sort(
            key=lambda item: _SOURCE_STRENGTH.get(str(item.get("source_type") or ""), 0),
            reverse=True,
        )
        chosen = dict(items[0])
        chosen["corroborating_sources"] = _unique(
            item.get("source_type") for item in items
        )
        chosen["raw_variants_in_group"] = _unique(
            item.get("raw_value")
            for item in values
            if item.get("address_group_key") == chosen.get("address_group_key")
        )
        selected.append(chosen)

    selected.sort(
        key=lambda item: _SOURCE_STRENGTH.get(str(item.get("source_type") or ""), 0),
        reverse=True,
    )
    return selected


def _address_group_key(components: dict[str, str], *, country_code: str) -> str:
    values = (
        country_code,
        components.get("house_number", ""),
        components.get("street", ""),
        components.get("unit", ""),
        components.get("city", ""),
        components.get("state", ""),
        components.get("postal_code", ""),
    )
    return "|".join(_component_key(value) for value in values)


def _country_code(value: Any) -> str:
    normalized = _normalize(value)
    if normalized in _US_COUNTRY_NAMES:
        return "US"
    if len(normalized) == 2:
        return normalized.upper()
    return normalized.upper().replace(" ", "_")


def _street_name(value: str) -> str:
    words = _clean(value).split()
    if len(words) < 2:
        return ""
    start = 1 if words[0].casefold().rstrip(".") in {"n", "s", "e", "w", "north", "south", "east", "west"} else 0
    return " ".join(words[start:-1])


def _street_suffix(value: str) -> str:
    words = _clean(value).split()
    return words[-1] if words else ""


def _component_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", _normalize(value))


def _normalize(value: Any) -> str:
    return re.sub(
        r"\s+", " ", unicodedata.normalize("NFKC", str(value or "")).strip(),
    ).casefold()


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split()).strip(" ,")


def _unique(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        raw = str(value or "").strip()
        key = _normalize(raw)
        if raw and key not in seen:
            seen.add(key)
            result.append(raw)
    return result
