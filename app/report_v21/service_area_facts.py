"""Source-aware service-area discovery, confirmation, and comparison.

Service coverage is an additive set, not a single identity value.  This module
therefore keeps candidate discovery separate from place confirmation, unions
confirmed places across trustworthy page sources, and retains rejected text
for diagnosis instead of allowing every short phrase after ``serving`` to
become an L2/L3 fact.
"""

from __future__ import annotations

import html
import re
import unicodedata
from typing import Any, Iterable


SCHEMA_VERSION = "1"

_SOURCE_STRENGTH = {
    "page.jsonld.area_served": 100,
    "page.dom.service_area_list": 90,
    "page.dom.service_area_statement": 80,
}
_SOURCE_LABELS = {
    "page.jsonld.area_served": "Target page · JSON-LD area served",
    "page.dom.service_area_list": "Target page · Service-area list",
    "page.dom.service_area_statement": "Target page · Service-area statement",
}

_SECTION_LABEL = re.compile(
    r"^(?:our\s+)?(?:service\s+areas?|areas?\s+(?:served|we\s+serve))\s*:?$",
    flags=re.IGNORECASE,
)
_SECTION_BOUNDARY = re.compile(
    r"^(?:services?|contact(?:\s+info|\s+us)?|about(?:\s+us)?|company|"
    r"business\s+hours?|opening\s+hours?|quick\s+links?|resources?|"
    r"terms(?:\s*(?:&|and)\s*conditions)?|privacy(?:\s+policy)?|"
    r"gallery|blog|news|careers?|financing|schedule|accessibility|"
    r"copyright(?:\s+.*)?|all\s+rights\s+reserved)\s*:?$",
    flags=re.IGNORECASE,
)
_SCOPE_PATTERNS = (
    re.compile(r"\bthroughout\s+(?P<scope>[^\n.!?]{1,320})", re.IGNORECASE),
    re.compile(r"\bacross\s+(?P<scope>[^\n.!?]{1,320})", re.IGNORECASE),
    re.compile(r"\bwe\s+serve\s+(?P<scope>[^\n.!?]{1,320})", re.IGNORECASE),
    re.compile(r"\bserves\s+(?P<scope>[^\n.!?]{1,320})", re.IGNORECASE),
    re.compile(r"\bserving\s+(?P<scope>[^\n.!?]{1,320})", re.IGNORECASE),
    re.compile(r"\bcovering\s+(?P<scope>[^\n.!?]{1,320})", re.IGNORECASE),
    re.compile(
        r"\b(?:service\s+areas?|areas?\s+served)\s*(?:include|includes|are|:|-)\s*"
        r"(?P<scope>[^\n.!?]{1,320})",
        re.IGNORECASE,
    ),
)
_HOME_SCOPE = re.compile(
    r"\byour\s+(?P<scope>[A-Z][^\n.!?]{2,320}?)\s+(?:home|homes|business|businesses|property|properties)\b",
    flags=re.IGNORECASE,
)
_AUDIENCE_PREFIX = re.compile(
    r"^(?:residential|commercial|light\s+commercial|homeowners?|business(?:es)?|"
    r"customers?|clients?|residents?|properties|homes?)\b",
    flags=re.IGNORECASE,
)
_SCOPE_TRAILING_BOUNDARY = re.compile(
    r"\s+(?:—|–|-|for\s+all\s+your|to\s+(?:schedule|book|discuss|request)|"
    r"designed\s+to|wherever\s+you|when\s+you)\b.*$",
    flags=re.IGNORECASE,
)

_STATE_NAMES_BY_CODE = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
    "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island",
    "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas",
    "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming", "DC": "District of Columbia",
}
_STATE_CODES_BY_NAME = {value.casefold(): key for key, value in _STATE_NAMES_BY_CODE.items()}

_NON_PLACE_VALUES = {
    "you", "your", "your area", "your home", "your business", "your property",
    "home", "homes", "business", "businesses", "property", "properties",
    "residential", "commercial", "customers", "clients", "residents",
    "homeowners", "nearby", "local", "all", "anywhere", "everywhere",
    "contact", "contact us", "contact info",
    "our services", "our service area", "the source", "the problem", "service area", "service areas",
}
_NON_PLACE_TOKENS = {
    "serve", "serves", "serving", "cover", "covers", "covering", "include", "includes",
    "including", "entire", "following", "around",
    "service", "services", "homeowners", "businesses", "customers", "clients",
    "residents", "residential", "commercial", "properties", "needs", "you", "your",
    "contact",
}
_BROAD_SUFFIXES = {"area", "metro", "region", "communities", "neighborhoods"}
_PLACE_SUFFIXES = {
    "borough", "boroughs", "city", "county", "counties", "district", "island",
    "parish", "town", "township", "village",
}
_SPECIAL_PHRASES = {
    "all 5 boroughs of nyc": "New York City",
    "all five boroughs of nyc": "New York City",
    "5 boroughs of nyc": "New York City",
    "five boroughs of nyc": "New York City",
    "all 5 boroughs of new york city": "New York City",
    "all five boroughs of new york city": "New York City",
    "nyc": "New York City",
    "tri state": "Tri-State",
    "tri state area": "Tri-State",
}
_HIERARCHY = {
    "new york": {
        "new york city", "long island", "nassau county", "suffolk county",
        "manhattan", "brooklyn", "queens", "bronx", "staten island",
    },
    "new york city": {"manhattan", "brooklyn", "queens", "bronx", "staten island"},
    "long island": {"nassau county", "suffolk county"},
    "tri state": {"new york", "new jersey", "connecticut", "pennsylvania"},
}


def build_service_area_facts(
    visible_text: str,
    jsonld_records: list[dict[str, Any]],
    *,
    source_url: str = "",
) -> dict[str, Any]:
    """Build a confirmed, additive page service-area fact set."""
    candidates: list[dict[str, Any]] = []
    candidates.extend(_jsonld_candidates(jsonld_records, source_url))
    lines = [_clean(line) for line in str(visible_text or "").splitlines() if _clean(line)]
    candidates.extend(_section_candidates(lines, source_url))
    candidates.extend(_statement_candidates(lines, source_url))
    candidates = _unique_observations(candidates)

    valid = [item for item in candidates if item.get("validation") == "valid"]
    rejected = [
        {**item, "eligible_for_l3": False}
        for item in candidates
        if item.get("validation") != "valid"
    ]
    accepted = _select_confirmed_union(valid)
    return {
        "schema_version": SCHEMA_VERSION,
        "service_areas": [str(item["value"]) for item in accepted],
        "observations": accepted,
        "candidate_observations": candidates,
        "rejected_observations": rejected,
    }


def compare_service_area_sets(
    page_values: list[str],
    gbp_values: list[str],
) -> tuple[str, str, str, list[dict[str, str]]]:
    """Compare confirmed place sets with aliases and known containment."""
    page_by_key = _comparison_places(page_values)
    gbp_by_key = _comparison_places(gbp_values)
    page_keys = set(page_by_key)
    gbp_keys = set(gbp_by_key)

    pairs: list[dict[str, str]] = []
    for page_key in sorted(page_keys):
        for gbp_key in sorted(gbp_keys):
            if _places_related(page_key, gbp_key):
                pairs.append({
                    "page_value": page_by_key[page_key],
                    "gbp_value": gbp_by_key[gbp_key],
                })

    exact_keys = page_keys == gbp_keys
    exact_text = exact_keys and all(
        _exact_text(page_by_key[key]) == _exact_text(gbp_by_key[key])
        for key in page_keys
    )
    if exact_text:
        return (
            "exact_match",
            "exact",
            "The checked page and GBP service-area sets are exactly equal.",
            _unique_pairs(pairs),
        )
    if not pairs:
        return (
            "material_conflict",
            "conflict",
            "The page and GBP expose specific service areas with no confirmed geographic overlap.",
            [],
        )

    page_covered = all(any(_places_related(key, other) for other in gbp_keys) for key in page_keys)
    gbp_covered = all(any(_places_related(key, other) for other in page_keys) for key in gbp_keys)
    if page_covered and gbp_covered:
        return (
            "semantic_match",
            "semantic",
            "The page and GBP service areas resolve to the same or geographically contained coverage.",
            _unique_pairs(pairs),
        )
    if page_covered:
        return (
            "semantic_match",
            "semantic",
            "The page service area is a compatible subset of the checked GBP coverage.",
            _unique_pairs(pairs),
        )
    return (
        "compatible_difference",
        "compatible",
        "The page and GBP service areas overlap; additional places are treated as compatible coverage differences.",
        _unique_pairs(pairs),
    )


def normalize_service_area(value: Any) -> str:
    """Return a stable comparison key for a service-area value."""
    canonical, _, _ = _canonical_place(value)
    text = canonical or _clean(value)
    normalized = re.sub(
        r"[^a-z0-9]+",
        " ",
        unicodedata.normalize("NFKC", text).casefold(),
    ).strip()
    normalized = re.sub(r"^(?:city of|greater)\s+", "", normalized)
    normalized = re.sub(r"\s+(?:area|metro|region)$", "", normalized)
    return normalized


def _jsonld_candidates(
    records: list[dict[str, Any]],
    source_url: str,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for record in records:
        values = record.get("areaServed")
        for item in values if isinstance(values, list) else [values]:
            raw = ""
            if isinstance(item, str):
                raw = item
            elif isinstance(item, dict):
                raw = str(item.get("name") or item.get("addressLocality") or "")
            candidates.append(_candidate(
                raw,
                "page.jsonld.area_served",
                source_url,
                "script[type='application/ld+json'] areaServed",
                str(item or ""),
                "authoritative",
            ))
    return [item for item in candidates if item]


def _section_candidates(lines: list[str], source_url: str) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for heading_index, heading in enumerate(lines):
        if not _SECTION_LABEL.fullmatch(heading):
            continue
        confirmed_started = False
        for index in range(heading_index + 1, min(len(lines), heading_index + 21)):
            raw = re.sub(r"^(?:[-*+•]|#{1,6})\s*", "", lines[index]).strip(" .:-")
            if not raw:
                continue
            if _SECTION_BOUNDARY.fullmatch(raw) or _looks_like_contact_boundary(raw):
                break
            item = _candidate(
                raw,
                "page.dom.service_area_list",
                source_url,
                f"visible line {index + 1} after service-area heading",
                f"{heading}: {raw}",
                "explicit",
            )
            if not item:
                continue
            candidates.append(item)
            if item.get("validation") == "valid":
                confirmed_started = True
            elif confirmed_started:
                break
    return candidates


def _statement_candidates(lines: list[str], source_url: str) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    windows = (
        (index, " ".join(lines[index:index + width]))
        for index in range(len(lines))
        for width in range(1, min(4, len(lines) - index) + 1)
    )
    for index, text in windows:
        for sentence in re.split(r"(?<=[.!?])\s+", text):
            for pattern in _SCOPE_PATTERNS:
                match = pattern.search(sentence)
                if not match:
                    continue
                scope = _trim_scope(match.group("scope"))
                if _AUDIENCE_PREFIX.search(scope) and not re.search(
                    r"\b(?:throughout|across)\b", match.group(0), re.IGNORECASE,
                ):
                    candidates.append(_candidate(
                        scope,
                        "page.dom.service_area_statement",
                        source_url,
                        f"visible line {index + 1}",
                        match.group(0),
                        "explicit",
                        forced_reason="audience_description_not_place_list",
                    ))
                    break
                for raw in _split_scope(scope):
                    candidates.append(_candidate(
                        raw,
                        "page.dom.service_area_statement",
                        source_url,
                        f"visible line {index + 1}",
                        match.group(0),
                        "explicit",
                    ))
                break
            home_match = _HOME_SCOPE.search(sentence)
            if home_match:
                scope = _trim_scope(home_match.group("scope"))
                for raw in _split_scope(scope):
                    candidates.append(_candidate(
                        raw,
                        "page.dom.service_area_statement",
                        source_url,
                        f"visible line {index + 1}",
                        home_match.group(0),
                        "explicit",
                    ))
    return [item for item in candidates if item]


def _split_scope(value: str) -> list[str]:
    text = re.sub(
        r"\ball\s+(?:5|five)\s+boroughs\s+of\s+(?:nyc|new\s+york\s+city)\b",
        "New York City",
        value,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\s*,?\s*\b(?:including|and|or)\b\s+", ", ", text, flags=re.IGNORECASE)
    comma_parts = [_clean(item).strip(" .:-") for item in text.split(",") if _clean(item).strip(" .:-")]
    if len(comma_parts) == 2:
        locality = comma_parts[0]
        region = comma_parts[1]
        locality_is_state = (
            locality.upper().rstrip(".") in _STATE_NAMES_BY_CODE
            or locality.casefold() in _STATE_CODES_BY_NAME
        )
        if not locality_is_state and (
            region.upper().rstrip(".") in _STATE_NAMES_BY_CODE
            or region.casefold() in _STATE_CODES_BY_NAME
        ):
            return [", ".join(comma_parts)]
    return [_clean(item).strip(" .:-") for item in re.split(r"[,/]", text) if _clean(item).strip(" .:-")]


def _trim_scope(value: str) -> str:
    text = _clean(value)
    text = _SCOPE_TRAILING_BOUNDARY.sub("", text)
    return text.strip(" .,:;-")


def _candidate(
    raw_value: Any,
    source_type: str,
    source_url: str,
    locator: str,
    excerpt: str,
    confidence: str,
    *,
    forced_reason: str | None = None,
) -> dict[str, Any]:
    raw = _clean(raw_value).strip(" .,:;-")
    if not raw:
        return {}
    canonical, place_kind, reason = _canonical_place(raw)
    reason = forced_reason or reason
    valid = bool(canonical and not reason)
    return {
        "value": canonical or raw,
        "raw_value": raw,
        "normalized_value": normalize_service_area(canonical or raw) if valid else "",
        "place_kind": place_kind,
        "source": source_type,
        "source_type": source_type,
        "source_label": _SOURCE_LABELS[source_type],
        "source_system": "page",
        "scope": "target_page",
        "source_scope": "target_page",
        "source_url": source_url or None,
        "locator": locator,
        "excerpt": _clean(excerpt)[:500] or None,
        "visibility": "structured" if ".jsonld." in source_type else "visible",
        "confidence": confidence,
        "validation": "valid" if valid else "rejected",
        "eligible_for_l2": valid,
        "eligible_for_l3": valid,
        "rejection_reason": reason,
    }


def _canonical_place(value: Any) -> tuple[str, str, str | None]:
    raw = _clean(value).strip(" .,:;-")
    if not raw:
        return "", "unknown", "empty_value"
    folded = _key(raw)
    if folded in _SPECIAL_PHRASES:
        canonical = _SPECIAL_PHRASES[folded]
        return canonical, "region" if canonical == "Tri-State" else "city", None

    raw = re.sub(r"^(?:including\s+|the\s+|greater\s+|surrounding\s+)+", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"\s+(?:communities|neighborhoods)$", "", raw, flags=re.IGNORECASE).strip()
    folded = _key(raw)
    if folded in _NON_PLACE_VALUES:
        return "", "unknown", "pronoun_or_generic_not_place"
    tokens = folded.split()
    if not tokens or any(token in _NON_PLACE_TOKENS for token in tokens):
        return "", "unknown", "non_place_language"
    if len(tokens) > 7:
        return "", "unknown", "place_value_too_long"
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 .,'&\-/]*", raw):
        return "", "unknown", "invalid_place_shape"

    comma_parts = [_clean(item) for item in raw.split(",") if _clean(item)]
    if len(comma_parts) == 2:
        locality, region = comma_parts
        region_key = region.casefold().rstrip(".")
        state_code = region.upper().rstrip(".")
        if state_code in _STATE_NAMES_BY_CODE or region_key in _STATE_CODES_BY_NAME:
            if _key(locality) in {"new york", "nyc"} and (
                state_code == "NY" or region_key == "new york"
            ):
                return "New York City", "city", None
            return _title_place(locality), _place_kind(locality), None

    upper = raw.upper().rstrip(".")
    if upper in _STATE_NAMES_BY_CODE:
        return _STATE_NAMES_BY_CODE[upper], "state", None
    if folded in _STATE_CODES_BY_NAME:
        return _title_place(raw), "state", None
    if folded in _SPECIAL_PHRASES:
        canonical = _SPECIAL_PHRASES[folded]
        return canonical, "region" if canonical == "Tri-State" else "city", None

    meaningful = [token for token in tokens if token not in {"of", "the"}]
    if not meaningful:
        return "", "unknown", "non_place_language"
    place_kind = _place_kind(raw)
    # A generic locality may be a single city name. Require title/upper case
    # unless a geographic suffix already makes the intent explicit.
    if place_kind == "locality" and not _looks_named(raw):
        return "", "unknown", "not_place_like"
    return _title_place(raw), place_kind, None


def _place_kind(value: str) -> str:
    tokens = _key(value).split()
    if not tokens:
        return "unknown"
    if tokens[-1] in {"county", "counties", "parish"}:
        return "county"
    if tokens[-1] in {"borough", "boroughs", "city", "town", "township", "village"}:
        return "city"
    if tokens[-1] in {"island", "area", "metro", "region"} or "tri state" in " ".join(tokens):
        return "region"
    if " ".join(tokens) in _STATE_CODES_BY_NAME:
        return "state"
    return "locality"


def _looks_named(value: str) -> bool:
    words = re.findall(r"[A-Za-z][A-Za-z.'-]*|[A-Z]{2,}", value)
    return bool(words) and all(
        word.casefold() in {"of", "the"}
        or word.isupper()
        or word[0].isupper()
        for word in words
    )


def _select_confirmed_union(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for item in values:
        key = str(item.get("normalized_value") or "")
        if not key:
            continue
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(item)

    selected: list[dict[str, Any]] = []
    for key in order:
        items = groups[key]
        items.sort(
            key=lambda item: _SOURCE_STRENGTH.get(str(item.get("source_type") or ""), 0),
            reverse=True,
        )
        chosen = dict(items[0])
        chosen["eligible_for_l2"] = True
        chosen["eligible_for_l3"] = True
        chosen["corroborating_sources"] = _unique(
            str(item.get("source_type") or "") for item in items
        )
        chosen["raw_variants"] = _unique(str(item.get("raw_value") or "") for item in items)
        selected.append(chosen)
    return selected


def _comparison_places(values: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        for item in _split_scope(str(value)):
            canonical, _, reason = _canonical_place(item)
            if canonical and not reason:
                result.setdefault(normalize_service_area(canonical), str(value))
    return result


def _places_related(left: str, right: str) -> bool:
    if left == right:
        return True
    return right in _descendants(left) or left in _descendants(right)


def _descendants(value: str) -> set[str]:
    result: set[str] = set()
    pending = list(_HIERARCHY.get(value, set()))
    while pending:
        child = pending.pop()
        if child in result:
            continue
        result.add(child)
        pending.extend(_HIERARCHY.get(child, set()))
    return result


def _looks_like_contact_boundary(value: str) -> bool:
    return bool(re.search(
        r"(?:@|\b(?:phone|tel|call|fax)\b|\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\b)",
        value,
        flags=re.IGNORECASE,
    ))


def _title_place(value: str) -> str:
    words = value.split()
    return " ".join(
        word.upper() if word.upper().rstrip(".") in _STATE_NAMES_BY_CODE else (
            word.casefold() if word.casefold() in {"of", "the"} else word[:1].upper() + word[1:]
        )
        for word in words
    )


def _exact_text(value: Any) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(value or "")).strip())


def _key(value: Any) -> str:
    return re.sub(
        r"[^a-z0-9]+",
        " ",
        unicodedata.normalize("NFKC", str(value or "")).casefold(),
    ).strip()


def _clean(value: Any) -> str:
    return " ".join(html.unescape(str(value or "")).replace("\xa0", " ").split()).strip()


def _unique(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        key = _key(value)
        if key and key not in seen:
            seen.add(key)
            result.append(value)
    return result


def _unique_observations(values: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in values:
        if not item:
            continue
        key = (
            str(item.get("source_type") or ""),
            _key(item.get("raw_value")),
            str(item.get("rejection_reason") or ""),
        )
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def _unique_pairs(values: Iterable[dict[str, str]]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in values:
        key = (item["page_value"], item["gbp_value"])
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result
