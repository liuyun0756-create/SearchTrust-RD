"""Deterministic, provenance-preserving page facts used by L3 rules 26-29.

The extractor deliberately separates *candidates* from facts that are eligible
for comparison.  Values found in URLs, scripts, discovery pages, or invalid
field shapes may be retained in ``rejected_observations`` for diagnosis, but
they can never silently enter the L3 comparison arrays.
"""

from __future__ import annotations

import html
import json
import re
import unicodedata
from typing import Any, Iterable

from app.report_v21.hours_facts import extract_page_hours_facts

try:  # Kept optional for stored-worker compatibility during rolling deploys.
    import phonenumbers
except ImportError:  # pragma: no cover - deployment installs requirements.txt
    phonenumbers = None  # type: ignore[assignment]


_ADDRESS_PATTERN = re.compile(
    r"\b\d{1,6}\s+(?:[NSEW]\.?\s+)?[A-Za-z0-9.' -]{2,60}?\s+"
    r"(?:Street|St\.?|Avenue|Ave\.?|Road|Rd\.?|Boulevard|Blvd\.?|Drive|Dr\.?|"
    r"Lane|Ln\.?|Court|Ct\.?|Highway|Hwy\.?|Way|Place|Pl\.?)(?![A-Za-z])"
    r"(?:\s*,?\s*(?:Suite|Ste\.?|Unit|#)\s*[A-Za-z0-9-]+)?"
    r"(?:\s*,\s*[A-Za-z .'-]{2,40}\s*,\s*[A-Z]{2}\s+\d{5}(?:-\d{4})?)?",
    flags=re.IGNORECASE,
)
_STREET_ADDRESS_PATTERN = re.compile(
    r"\b\d{1,6}\s+(?:[NSEW]\.?\s+)?[A-Za-z0-9.'# -]{2,70}?\s+"
    r"(?:Street|St\.?|Avenue|Ave\.?|Road|Rd\.?|Boulevard|Blvd\.?|Drive|Dr\.?|"
    r"Lane|Ln\.?|Court|Ct\.?|Highway|Hwy\.?|Way|Place|Pl\.?|Parkway|Pkwy\.?)\b"
    r"(?:\s*(?:Suite|Ste\.?|Unit|#)\s*[A-Za-z0-9-]+)?",
    flags=re.IGNORECASE,
)
_LOCALITY_REGION_POSTAL_PATTERN = re.compile(
    r"\b[A-Za-z][A-Za-z .'-]{1,50},?\s+[A-Z]{2}\s+\d{5}(?:-\d{4})?\b"
)
_MAP_ANCHOR_PATTERN = re.compile(
    r"<a\b[^>]*href=[\"'](?P<href>[^\"']*(?:google\.[^\"']*/maps|goo\.gl/maps|"
    r"maps\.app\.goo\.gl)[^\"']*)[\"'][^>]*>(?P<body>.*?)</a\s*>",
    flags=re.IGNORECASE | re.DOTALL,
)
_HOURS_PATTERNS = (
    re.compile(r"\b24\s*/\s*7\b", flags=re.IGNORECASE),
    re.compile(r"\bopen\s+24\s+hours?\b", flags=re.IGNORECASE),
    re.compile(
        r"\b(?:mon(?:day)?|tue(?:sday)?|wed(?:nesday)?|thu(?:rsday)?|fri(?:day)?|"
        r"sat(?:urday)?|sun(?:day)?)\b[^\n]{0,80}\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\b",
        flags=re.IGNORECASE,
    ),
)
_SERVICE_PREFIX = re.compile(
    r"\b(?:service\s+areas?|areas?\s+served|serving|we\s+serve|covering)\b\s*[:\-]?\s*(.+)",
    flags=re.IGNORECASE,
)
_EXPLICIT_SERVICE_AREA_SENTENCE = re.compile(
    r"\byour\s+(?P<areas>[A-Z][A-Za-z .'-]*(?:\s*,\s*[A-Z][A-Za-z .'-]*){1,}"
    r"(?:\s*,?\s*(?:or|and)\s+[A-Z][A-Za-z .'-]*)?)\s+homes?\b",
    flags=re.IGNORECASE,
)
_SELF_IDENTIFICATION = re.compile(
    r"(?:^|\n|[.!?]\s+)\s*"
    r"([A-Z][A-Za-z0-9&.\-'\u2019]*(?:\s+[A-Z][A-Za-z0-9&.\-'\u2019]*){0,5})\s+"
    r"is\s+(?:an?\s+)?(?:leading\s+|local\s+|professional\s+|trusted\s+|"
    r"family[- ]owned\s+)*(?:[a-z][a-z\-'\u2019]*\s+){0,4}"
    r"(?:company|business|contractor|provider)\b",
    flags=re.MULTILINE,
)
_FOOTER_OWNER = re.compile(
    r"(?:Copyright\s*)?(?:\u00a9|@)\s*\d{4}\s+"
    r"([A-Za-z0-9][A-Za-z0-9\s&.\-'\u2019]{1,60}?)"
    r"(?=\s+(?:[-\u2013\u2014|]\s+)|\s+All\b|\s*\.|$|\n)",
    flags=re.IGNORECASE,
)
_JSON_LD_RE = re.compile(
    r"<script\b[^>]*type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script\s*>",
    flags=re.IGNORECASE | re.DOTALL,
)
_SCRIPT_STYLE_RE = re.compile(
    r"<(?:script|style|noscript|template|svg)\b[^>]*>.*?</(?:script|style|noscript|template|svg)\s*>",
    flags=re.IGNORECASE | re.DOTALL,
)
_HTML_TAG_RE = re.compile(r"<[^>]+>", flags=re.DOTALL)
_MARKDOWN_LINK_RE = re.compile(r"!?\[([^\]]*)\]\(([^)]+)\)")
_RAW_URL_RE = re.compile(r"(?:https?|ftp)://[^\s<>'\"]+", flags=re.IGNORECASE)

_BUSINESS_TYPES = {
    "localbusiness",
    "organization",
    "plumber",
    "homeandconstructionbusiness",
    "professionalservice",
}
_GENERIC_NAMES = {
    "a", "all", "an", "and", "about", "business", "company", "contact", "for",
    "home", "local", "logo", "official", "our", "page", "service", "services",
    "rights", "reserved", "site", "the", "website", "welcome", "with", "your",
}
_THIRD_PARTY_OR_DESCRIPTOR_NAMES = {
    "facebook", "google", "instagram", "linkedin", "pinterest", "tiktok",
    "twitter", "yelp", "youtube", "colorful", "monochrome", "review", "reviews",
}
_NAME_SOURCE_PRIORITY = {
    "page.jsonld.local_business.name": 100,
    "page.dom.visible_brand": 90,
    "page.dom.logo_alt": 85,
    "page.meta.og_site_name": 80,
    "page.dom.self_identification": 75,
    "page.dom.footer_owner": 70,
    "page.meta.title_brand": 60,
    "page.scraper.selected_name": 50,
}
_PHONE_SOURCE_PRIORITY = {
    "page.dom.tel_href": 100,
    "page.jsonld.telephone": 95,
    "page.jsonld.contact_point.telephone": 90,
    "page.dom.visible_phone_labeled": 80,
    "page.dom.visible_phone": 60,
    "page.scraper.selected_phone": 40,
}
_ADDRESS_SOURCE_PRIORITY = {
    "page.jsonld.postal_address": 100,
    "page.dom.map_address": 95,
    "page.dom.address_element": 90,
    "page.dom.visible_address_labeled": 80,
    "page.dom.visible_address": 60,
}
_SERVICE_SOURCE_PRIORITY = {
    "page.jsonld.area_served": 100,
    "page.dom.service_area_section": 80,
}
_SIGNAL_SOURCE_MAP = {
    "json_ld": "page.jsonld.local_business.name",
    "visible_brand_heading": "page.dom.visible_brand",
    "logo_alt": "page.dom.logo_alt",
    "og_site_name": "page.meta.og_site_name",
    "title_suffix": "page.meta.title_brand",
    "title_prefix": "page.meta.title_brand",
    "copyright_owner": "page.dom.footer_owner",
    "visible_brand_statement": "page.dom.visible_brand",
}
_SOURCE_LABELS = {
    "page.jsonld.local_business.name": "Target page · JSON-LD business name",
    "page.jsonld.telephone": "Target page · JSON-LD telephone",
    "page.jsonld.contact_point.telephone": "Target page · JSON-LD contact telephone",
    "page.jsonld.postal_address": "Target page · JSON-LD postal address",
    "page.jsonld.area_served": "Target page · JSON-LD area served",
    "page.dom.tel_href": "Target page · Phone link (tel:)",
    "page.dom.visible_phone_labeled": "Target page · Labeled visible phone",
    "page.dom.visible_phone": "Target page · Visible phone",
    "page.dom.address_element": "Target page · Address element",
    "page.dom.visible_address_labeled": "Target page · Labeled visible address",
    "page.dom.visible_address": "Target page · Visible address",
    "page.dom.map_address": "Target page · Complete visible map address",
    "page.dom.service_area_section": "Target page · Service-area section",
    "page.dom.visible_brand": "Target page · Visible brand",
    "page.dom.logo_alt": "Target page · Logo text",
    "page.meta.og_site_name": "Target page · Site-name metadata",
    "page.dom.self_identification": "Target page · Self-identification text",
    "page.dom.footer_owner": "Target page · Footer owner",
}


def build_page_facts(
    content: str,
    business: Any = None,
    identity_signals: Any = None,
    *,
    structured_content: str = "",
    source_url: str = "",
) -> dict[str, Any]:
    """Build target-page facts and an auditable candidate ledger.

    ``content`` is the readable page representation. ``structured_content`` is
    optional rendered/raw HTML from the same URL. It is never allowed to bring
    supporting-page or GBP values into the target-page fact set.
    """
    text = str(content or "")
    structured = str(structured_content or "")
    business_record = business if isinstance(business, dict) else {}
    visible_text = _visible_text(text)

    jsonld_records = _jsonld_business_records(f"{structured}\n{text}")
    candidates = {
        "business_names": _business_name_candidates(
            text, visible_text, structured, business_record, identity_signals,
            jsonld_records, source_url,
        ),
        "phones": _phone_candidates(
            text, visible_text, structured, business_record, jsonld_records, source_url,
        ),
        "addresses": _address_candidates(visible_text, structured, jsonld_records, source_url),
        "service_areas": _service_area_candidates(visible_text, jsonld_records, source_url),
    }

    priorities = {
        "business_names": _NAME_SOURCE_PRIORITY,
        "phones": _PHONE_SOURCE_PRIORITY,
        "addresses": _ADDRESS_SOURCE_PRIORITY,
        "service_areas": _SERVICE_SOURCE_PRIORITY,
    }
    accepted: dict[str, list[dict[str, Any]]] = {}
    rejected: dict[str, list[dict[str, Any]]] = {}
    for field, field_candidates in candidates.items():
        if field == "business_names":
            selected, excluded = _select_name_eligible(field_candidates, priorities[field])
        else:
            selected, excluded = _select_eligible(field_candidates, priorities[field])
        accepted[field] = selected
        rejected[field] = excluded

    opening_hours = extract_page_hours_facts(
        text,
        structured,
        source_url=source_url,
    )
    hours = _unique(
        item.get("raw_value") or item.get("value")
        for item in opening_hours.get("observations", [])
        if isinstance(item, dict)
    )
    # Keep the legacy bounded patterns as a compatibility fallback when a
    # provider returns readable text that the richer parser cannot classify.
    if not hours:
        hours = _unique(
            match.group(0)
            for pattern in _HOURS_PATTERNS
            for match in pattern.finditer(visible_text)
        )
    accepted["hours"] = list(opening_hours.get("observations") or [])
    return {
        "version": "4",
        "business_names": _observation_values(accepted["business_names"]),
        "addresses": _observation_values(accepted["addresses"]),
        "phones": _observation_values(accepted["phones"]),
        "service_areas": _observation_values(accepted["service_areas"]),
        "hours": hours,
        "opening_hours": opening_hours,
        "observations": accepted,
        "candidate_observations": {
            field: _unique_observations(values) for field, values in candidates.items()
        },
        "rejected_observations": rejected,
    }


def _business_name_candidates(
    content: str,
    visible_text: str,
    structured: str,
    business: dict[str, Any],
    identity_signals: Any,
    records: list[dict[str, Any]],
    source_url: str,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for record in records:
        _append_name_candidate(
            candidates, record.get("name"), "page.jsonld.local_business.name",
            source_url, "script[type='application/ld+json']", "JSON-LD business name",
        )

    if isinstance(identity_signals, list):
        for signal in identity_signals:
            if not isinstance(signal, dict) or str(signal.get("field") or "") != "name":
                continue
            if str(signal.get("scope") or "target_page") not in {"page", "target_page"}:
                continue
            source_type = _SIGNAL_SOURCE_MAP.get(str(signal.get("source") or ""))
            if source_type:
                _append_name_candidate(
                    candidates, signal.get("value"), source_type, source_url,
                    str(signal.get("source") or "identity_signal"),
                    str(signal.get("value") or ""),
                )

    og_patterns = (
        r'<meta[^>]+property=["\']og:site_name["\'][^>]+content=["\']([^"\']+)',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:site_name["\']',
    )
    for pattern in og_patterns:
        match = re.search(pattern, structured, re.IGNORECASE)
        if match:
            _append_name_candidate(
                candidates, match.group(1), "page.meta.og_site_name", source_url,
                "meta[property='og:site_name']", match.group(0),
            )
            break

    for tag in re.findall(r"<img\b[^>]*>", structured, re.IGNORECASE | re.DOTALL):
        alt_match = re.search(r'\balt=["\']([^"\']+)["\']', tag, re.IGNORECASE)
        if not alt_match or "logo" not in f"{tag} {alt_match.group(1)}".casefold():
            continue
        value = re.sub(
            r"\b(?:official|company|business)?\s*logo\b", " ",
            html.unescape(alt_match.group(1)), flags=re.IGNORECASE,
        ).strip(" -|:_")
        _append_name_candidate(
            candidates, value, "page.dom.logo_alt", source_url,
            "img[alt]", alt_match.group(1),
        )

    self_match = _SELF_IDENTIFICATION.search(visible_text)
    if self_match:
        _append_name_candidate(
            candidates, self_match.group(1), "page.dom.self_identification",
            source_url, "visible text", self_match.group(0),
        )
    footer_match = _FOOTER_OWNER.search(visible_text)
    if footer_match:
        _append_name_candidate(
            candidates, footer_match.group(1), "page.dom.footer_owner",
            source_url, "footer/copyright", footer_match.group(0),
        )

    visible_brand_patterns = (
        r"\b([A-Z][A-Za-z0-9&.\-'\u2019]*(?:[ \t]+[A-Z][A-Za-z0-9&.\-'\u2019]*){0,5})[ \t]*,[ \t]+(?:the|a)[ \t]+(?:top|leading|trusted|local|professional)\b",
        r"\b([A-Z][A-Za-z0-9&.\-'\u2019]*(?:[ \t]+[A-Z][A-Za-z0-9&.\-'\u2019]*){0,5})[ \t]+is[ \t]+(?:proud|prepared|ready)\b",
        r"\bAll\s+Rights\s+Reserved\s*\|\s*([^|\n]{2,60})\s*\|",
    )
    for pattern in visible_brand_patterns:
        flags = re.IGNORECASE if "All\\s+Rights" in pattern else 0
        for match in re.finditer(pattern, visible_text, flags):
            _append_name_candidate(
                candidates, match.group(1), "page.dom.visible_brand", source_url,
                "visible brand statement", match.group(0),
            )

    for match in re.finditer(r"\bContact[ \t]+\[([^\]\n]{2,60})\]\([^)]+\)", content):
        _append_name_candidate(
            candidates, match.group(1), "page.dom.visible_brand", source_url,
            "visible contact brand link", match.group(0),
        )

    first_line = next((line for line in visible_text.splitlines() if line.strip()), "")
    first_line = re.sub(r"^#{1,6}[ \t]+", "", first_line).strip()
    if " | " in first_line:
        for part in (part.strip() for part in first_line.split(" | ")):
            if part and not re.search(
                r"\b(?:services?|repairs?|installation|replacement|maintenance|emergency|city|near me)\b",
                part,
                re.IGNORECASE,
            ):
                _append_name_candidate(
                    candidates, part, "page.meta.title_brand", source_url,
                    "page title segment", first_line,
                )

    for alt, src in _MARKDOWN_LINK_RE.findall(content):
        if "logo" in f"{alt} {src}".casefold():
            value = re.sub(r"\b(?:official|company|business)?\s*logo\b", " ", alt, flags=re.IGNORECASE)
            _append_name_candidate(
                candidates, value.strip(" -|:_"), "page.dom.logo_alt", source_url,
                "img[alt]", alt,
            )

    # The scraper-selected name is only a last-resort target-page clue. Invalid
    # generic values such as "for" are still recorded as rejected candidates.
    value = business.get("name")
    if value and not candidates:
        _append_name_candidate(
            candidates, value, "page.scraper.selected_name", source_url,
            "scraper identity resolver", str(value),
        )
    return _unique_observations(candidates)


def _append_name_candidate(
    candidates: list[dict[str, Any]],
    value: Any,
    source_type: str,
    source_url: str,
    locator: str,
    excerpt: str,
) -> None:
    raw = " ".join(html.unescape(str(value or "")).split()).strip(" -|:")
    if not raw:
        return
    valid, reason = _validate_business_name(raw, source_type)
    candidates.append(_observation(
        raw, source_type, source_url=source_url, locator=locator, excerpt=excerpt,
        normalized_value=_normalize_name(raw), validation="valid" if valid else "rejected",
        rejection_reason=reason,
    ))


def _phone_candidates(
    content: str,
    visible_text: str,
    structured: str,
    business: dict[str, Any],
    records: list[dict[str, Any]],
    source_url: str,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []

    anchor_pattern = re.compile(
        r"<a\b[^>]*href=[\"']tel:([^\"']+)[\"'][^>]*>(.*?)</a\s*>",
        re.IGNORECASE | re.DOTALL,
    )
    consumed_tel_values: set[str] = set()
    for match in anchor_pattern.finditer(structured):
        destination = match.group(1).strip()
        label = _visible_text(match.group(2)).strip()
        raw = next(iter(_find_phone_strings(label)), "") or destination
        _append_phone_candidate(
            candidates, raw, "page.dom.tel_href", source_url,
            "a[href^='tel:']", _visible_text(match.group(0)) or match.group(0),
        )
        consumed_tel_values.add(destination)
    for match in re.finditer(r"href=[\"']tel:([^\"']+)[\"']", structured, re.IGNORECASE):
        if match.group(1).strip() in consumed_tel_values:
            continue
        _append_phone_candidate(
            candidates, match.group(1), "page.dom.tel_href", source_url,
            "a[href^='tel:']", match.group(0),
        )
    for label, destination in _MARKDOWN_LINK_RE.findall(content):
        if destination.strip().casefold().startswith("tel:"):
            raw = next(iter(_find_phone_strings(label)), "") or destination.strip()[4:]
            _append_phone_candidate(
                candidates, raw, "page.dom.tel_href", source_url,
                "markdown link with tel: destination", f"[{label}]({destination})",
            )

    for record in records:
        telephone = record.get("telephone")
        for value in telephone if isinstance(telephone, list) else [telephone]:
            _append_phone_candidate(
                candidates, value, "page.jsonld.telephone", source_url,
                "script[type='application/ld+json'] telephone", str(value or ""),
            )
        contact_points = record.get("contactPoint")
        for point in contact_points if isinstance(contact_points, list) else [contact_points]:
            if isinstance(point, dict):
                contact_type = str(point.get("contactType") or "").casefold()
                _append_phone_candidate(
                    candidates, point.get("telephone"), "page.jsonld.contact_point.telephone",
                    source_url, "JSON-LD contactPoint.telephone", str(point.get("telephone") or ""),
                    "secondary" if any(label in contact_type for label in ("fax", "sales")) else "primary",
                )

    lines = visible_text.splitlines()
    for index, line in enumerate(lines):
        if not re.search(r"\b(?:phone|tel(?:ephone)?|call)\b", line, re.IGNORECASE):
            continue
        comparison_role = "secondary" if re.search(r"\b(?:fax|sales)\b", line, re.IGNORECASE) else "primary"
        for raw in _find_phone_strings(line):
            _append_phone_candidate(
                candidates, raw, "page.dom.visible_phone_labeled", source_url,
                f"visible line {index + 1}", line, comparison_role,
            )

    # Standalone visible numbers are a bounded fallback. URLs and all HTML
    # attributes have already been removed by _visible_text. Explicit fax/sales
    # labels remain auditable but cannot become the primary identity phone.
    for index, line in enumerate(lines):
        comparison_role = "secondary" if re.search(r"\b(?:fax|sales)\b", line, re.IGNORECASE) else "primary"
        for raw in _find_phone_strings(line):
            _append_phone_candidate(
                candidates, raw, "page.dom.visible_phone", source_url,
                f"visible line {index + 1}", line, comparison_role,
            )

    if business.get("phone"):
        _append_phone_candidate(
            candidates, business.get("phone"), "page.scraper.selected_phone", source_url,
            "scraper identity resolver", str(business.get("phone")),
        )
    return _unique_observations(candidates)


def _append_phone_candidate(
    candidates: list[dict[str, Any]],
    value: Any,
    source_type: str,
    source_url: str,
    locator: str,
    excerpt: str,
    comparison_role: str = "primary",
) -> None:
    raw = html.unescape(str(value or "")).strip()
    if not raw:
        return
    extracted = _find_phone_strings(raw)
    if len(extracted) == 1:
        raw = extracted[0]
    normalized, reason = normalize_phone(raw)
    observation = _observation(
        raw, source_type, source_url=source_url, locator=locator, excerpt=excerpt,
        normalized_value=normalized, validation="valid" if normalized else "rejected",
        rejection_reason=reason,
    )
    observation["comparison_role"] = comparison_role
    candidates.append(observation)


def _address_candidates(
    visible_text: str,
    structured: str,
    records: list[dict[str, Any]],
    source_url: str,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for record in records:
        address = _format_postal_address(record.get("address"))
        if address:
            candidates.append(_observation(
                address, "page.jsonld.postal_address", source_url=source_url,
                locator="script[type='application/ld+json'] address", excerpt=address,
                normalized_value=_normalize_text(address), validation="valid",
            ))

    # Some page builders render one postal address as two visible anchors that
    # point to the same map destination (street on one line, locality/region/
    # postal code on the next).  They are one page fact, not two unrelated
    # guesses, so compose them before field validation and source selection.
    map_groups: dict[str, list[str]] = {}
    for match in _MAP_ANCHOR_PATTERN.finditer(structured):
        href = html.unescape(match.group("href")).strip()
        anchor_text = " ".join(_visible_text(match.group("body")).split())
        if href and anchor_text:
            map_groups.setdefault(href, []).append(anchor_text)
    for href, parts in map_groups.items():
        streets = _unique(
            match.group(0).strip()
            for part in parts
            for match in _STREET_ADDRESS_PATTERN.finditer(part)
        )
        localities = _unique(
            match.group(0).strip()
            for part in parts
            for match in _LOCALITY_REGION_POSTAL_PATTERN.finditer(part)
        )
        for street in streets:
            for locality in localities:
                raw = f"{street}, {locality}"
                candidates.append(_observation(
                    raw, "page.dom.map_address", source_url=source_url,
                    locator=f"map link {href}", excerpt=" | ".join(parts),
                    normalized_value=_normalize_text(raw), validation="valid",
                ))

    for index, match in enumerate(re.finditer(r"<address\b[^>]*>(.*?)</address\s*>", structured, re.IGNORECASE | re.DOTALL)):
        raw = _visible_text(match.group(1)).strip()
        if _valid_address(raw):
            candidates.append(_observation(
                raw, "page.dom.address_element", source_url=source_url,
                locator=f"address:nth-of-type({index + 1})", excerpt=raw,
                normalized_value=_normalize_text(raw), validation="valid",
            ))

    for index, line in enumerate(visible_text.splitlines()):
        labeled = bool(re.search(r"\b(?:address|located at|location)\b", line, re.IGNORECASE))
        for match in _ADDRESS_PATTERN.finditer(line):
            source_type = "page.dom.visible_address_labeled" if labeled else "page.dom.visible_address"
            candidates.append(_observation(
                match.group(0), source_type, source_url=source_url,
                locator=f"visible line {index + 1}", excerpt=line,
                normalized_value=_normalize_text(match.group(0)), validation="valid",
            ))
    return _unique_observations(candidates)


def _service_area_candidates(
    visible_text: str,
    records: list[dict[str, Any]],
    source_url: str,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for record in records:
        for raw in _area_served_values(record.get("areaServed")):
            if _valid_area_name(raw):
                candidates.append(_observation(
                    raw, "page.jsonld.area_served", source_url=source_url,
                    locator="script[type='application/ld+json'] areaServed", excerpt=raw,
                    normalized_value=_normalize_text(raw), validation="valid",
                ))

    for index, line in enumerate(visible_text.splitlines()):
        match = _SERVICE_PREFIX.search(line)
        if not match:
            continue
        tail = re.split(r"[.!?;|]", match.group(1), maxsplit=1)[0]
        # Narrative copy such as "serving homeowners and businesses in
        # Manhattan ... and the following communities" is not a field value.
        # Splitting that sentence would manufacture tokens like homeowners,
        # businesses, following communities, or a bare state abbreviation.
        # Fail closed unless the matched tail is an actual place list.
        if re.search(
            r"\b(?:homeowners?|business(?:es)?|customers?|clients?|residents?|"
            r"following|including|(?:the\s+)?entire|communities?\s+in|"
            r"in\s+and\s+around)\b",
            tail,
            flags=re.IGNORECASE,
        ):
            continue
        for candidate in re.split(r"\s*(?:,|\band\b|\bor\b|/)\s*", tail, flags=re.IGNORECASE):
            value = re.sub(
                r"^(?:the\s+)?(?:greater\s+)?|\s+(?:area|metro|region|communities|neighborhoods)$",
                "", candidate.strip(), flags=re.IGNORECASE,
            ).strip(" :-")
            if _valid_area_name(value):
                candidates.append(_observation(
                    value, "page.dom.service_area_section", source_url=source_url,
                    locator=f"visible line {index + 1}", excerpt=line,
                    normalized_value=_normalize_text(value), validation="valid",
                ))

    # Explicit natural-language coverage statements are also page facts.  A
    # rendered paragraph may be wrapped across adjacent lines, so use bounded
    # windows rather than joining the whole page and crossing section borders.
    lines = [line.strip() for line in visible_text.splitlines() if line.strip()]
    windows = (
        (index, " ".join(lines[index:index + width]))
        for index in range(len(lines))
        for width in range(1, min(4, len(lines) - index) + 1)
    )
    for index, sentence in windows:
        for match in _EXPLICIT_SERVICE_AREA_SENTENCE.finditer(sentence):
            for value in _explicit_area_names(match.group("areas")):
                candidates.append(_observation(
                    value, "page.dom.service_area_section", source_url=source_url,
                    locator=f"visible line {index + 1}", excerpt=match.group(0),
                    normalized_value=_normalize_text(value), validation="valid",
                ))
    return _unique_observations(candidates)


def _explicit_area_names(value: str) -> list[str]:
    separated = re.sub(
        r"\s*,?\s*\b(?:or|and)\b\s+", ", ", value, flags=re.IGNORECASE,
    )
    names: list[str] = []
    for item in separated.split(","):
        name = re.sub(r"\s+", " ", item).strip(" .:-")
        if (
            _valid_area_name(name)
            and re.fullmatch(
                r"[A-Z][A-Za-z.'-]*(?:\s+[A-Z][A-Za-z.'-]*){0,3}", name,
            )
        ):
            names.append(name)
    return _unique(names)


def _select_eligible(
    candidates: list[dict[str, Any]],
    priorities: dict[str, int],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    valid = [
        item for item in candidates
        if item.get("validation") == "valid" and item.get("comparison_role", "primary") == "primary"
    ]
    if not valid:
        return [], [_mark_rejected(item, item.get("rejection_reason") or "validation_failed") for item in candidates]

    best_priority = max(priorities.get(str(item.get("source_type") or ""), 0) for item in valid)
    selected: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for item in candidates:
        priority = priorities.get(str(item.get("source_type") or ""), 0)
        if (
            item.get("validation") == "valid"
            and item.get("comparison_role", "primary") == "primary"
            and priority == best_priority
        ):
            selected.append({**item, "eligible_for_l3": True})
        else:
            reason = item.get("rejection_reason")
            if not reason and item.get("comparison_role") == "secondary":
                reason = "secondary_phone_not_primary_identity"
            reason = reason or "lower_priority_source"
            rejected.append(_mark_rejected(item, reason))
    return _unique_observations(selected), _unique_observations(rejected)


def _select_name_eligible(
    candidates: list[dict[str, Any]],
    priorities: dict[str, int],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Prefer a corroborated brand over an isolated high-priority assertion."""
    valid = [item for item in candidates if item.get("validation") == "valid"]
    if not valid:
        return [], [_mark_rejected(item, item.get("rejection_reason") or "validation_failed") for item in candidates]

    groups: dict[str, list[dict[str, Any]]] = {}
    for item in valid:
        key = _normalize_text(str(item.get("value") or ""))
        groups.setdefault(key, []).append(item)

    def score(items: list[dict[str, Any]]) -> tuple[int, int, int]:
        source_count = len({str(item.get("source_type") or "") for item in items})
        max_priority = max(priorities.get(str(item.get("source_type") or ""), 0) for item in items)
        return source_count, len(items), max_priority

    best_score = max(score(items) for items in groups.values())
    winning_keys = {key for key, items in groups.items() if score(items) == best_score}
    selected: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for item in candidates:
        key = _normalize_text(str(item.get("value") or ""))
        if item.get("validation") == "valid" and key in winning_keys:
            selected.append({**item, "eligible_for_l3": True})
        else:
            rejected.append(_mark_rejected(
                item,
                str(item.get("rejection_reason") or "uncorroborated_or_lower_priority_name"),
            ))

    # One raw value per winning normalized brand; prefer its strongest source.
    canonical: list[dict[str, Any]] = []
    for key in winning_keys:
        items = [item for item in selected if _normalize_text(str(item.get("value") or "")) == key]
        items.sort(key=lambda item: priorities.get(str(item.get("source_type") or ""), 0), reverse=True)
        canonical.append(items[0])
    return _unique_observations(canonical), _unique_observations(rejected)


def _mark_rejected(item: dict[str, Any], reason: str) -> dict[str, Any]:
    return {
        **item,
        "eligible_for_l3": False,
        "rejection_reason": str(reason),
    }


def _observation(
    value: Any,
    source_type: str,
    *,
    source_url: str = "",
    locator: str = "",
    excerpt: str = "",
    normalized_value: str = "",
    validation: str = "valid",
    rejection_reason: str | None = None,
) -> dict[str, Any]:
    raw = str(value or "").strip()
    if not raw:
        return {}
    return {
        "value": raw,
        "raw_value": raw,
        "normalized_value": normalized_value,
        "source": source_type,
        "source_type": source_type,
        "source_label": _SOURCE_LABELS.get(source_type, source_type),
        "source_system": "page",
        "scope": "target_page",
        "source_scope": "target_page",
        "source_url": source_url or None,
        "locator": locator or None,
        "excerpt": " ".join(str(excerpt or "").split())[:300] or None,
        "visibility": "structured" if ".jsonld." in source_type else "visible",
        "validation": validation,
        "eligible_for_l3": validation == "valid",
        "rejection_reason": rejection_reason,
    }


def _visible_text(value: str) -> str:
    """Return text nodes while excluding URL/attribute/script contamination."""
    text = _SCRIPT_STYLE_RE.sub("\n", str(value or ""))
    text = _MARKDOWN_LINK_RE.sub(lambda match: match.group(1) if not match.group(0).startswith("!") else " ", text)
    text = _RAW_URL_RE.sub(" ", text)
    text = _HTML_TAG_RE.sub(" ", text)
    text = html.unescape(text).replace("\xa0", " ")
    return "\n".join(re.sub(r"[\t \f\v]+", " ", line).strip() for line in text.splitlines())


def _jsonld_business_records(value: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for raw in _JSON_LD_RE.findall(value or ""):
        try:
            parsed = json.loads(html.unescape(raw).strip())
        except (json.JSONDecodeError, TypeError):
            continue
        for record in _schema_records(parsed):
            raw_types = record.get("@type")
            types = raw_types if isinstance(raw_types, list) else [raw_types]
            normalized_types = {str(item).strip().casefold() for item in types if item}
            if normalized_types.intersection(_BUSINESS_TYPES):
                records.append(record)
    return records


def _schema_records(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        graph = value.get("@graph")
        if isinstance(graph, list):
            for item in graph:
                yield from _schema_records(item)
    elif isinstance(value, list):
        for item in value:
            yield from _schema_records(item)


def _format_postal_address(value: Any) -> str:
    if isinstance(value, str):
        return " ".join(value.split())
    if not isinstance(value, dict):
        return ""
    parts = [
        value.get("streetAddress"),
        value.get("addressLocality"),
        value.get("addressRegion"),
        value.get("postalCode"),
        value.get("addressCountry"),
    ]
    normalized_parts = [
        " ".join(str(part).split()).strip(" ,")
        for part in parts
        if str(part or "").strip(" ,")
    ]
    return ", ".join(normalized_parts)


def _area_served_values(value: Any) -> list[str]:
    values = value if isinstance(value, list) else [value]
    result: list[str] = []
    for item in values:
        if isinstance(item, str):
            result.append(item.strip())
        elif isinstance(item, dict):
            name = str(item.get("name") or item.get("addressLocality") or "").strip()
            if name:
                result.append(name)
    return result


def _find_phone_strings(value: str) -> list[str]:
    if phonenumbers is not None:
        return [match.raw_string for match in phonenumbers.PhoneNumberMatcher(value, "US")]
    # Rolling-deploy fallback only; the normal runtime uses libphonenumber.
    pattern = re.compile(r"(?<!\d)(?:\+?1[\s.()-]*)?(?:\(?\d{3}\)?[\s.-]*)\d{3}[\s.-]*\d{4}(?!\d)")
    return [match.group(0) for match in pattern.finditer(value)]


def normalize_phone(value: str, default_region: str = "US") -> tuple[str, str | None]:
    """Return E.164 for a valid phone or a deterministic rejection reason."""
    raw = html.unescape(str(value or "")).strip()
    if not raw:
        return "", "empty_value"
    if re.search(r"[-.]{2,}|[-.]\s*[-.]", raw):
        return "", "malformed_separators"
    if phonenumbers is None:
        digits = re.sub(r"\D", "", raw)
        if len(digits) == 11 and digits.startswith("1"):
            digits = digits[1:]
        return (f"+1{digits}", None) if len(digits) == 10 else ("", "invalid_phone")
    try:
        number = phonenumbers.parse(raw, None if raw.startswith("+") else default_region)
    except phonenumbers.NumberParseException:
        return "", "unparseable_phone"
    if not phonenumbers.is_possible_number(number):
        return "", "impossible_phone"
    if not phonenumbers.is_valid_number(number):
        return "", "invalid_phone"
    return phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164), None


def _validate_business_name(value: str, source_type: str = "") -> tuple[bool, str | None]:
    normalized = _normalize_text(value)
    words = re.findall(r"[A-Za-z0-9]+", normalized)
    if not words or len(normalized) < 2:
        return False, "name_too_short"
    if len(words) == 1 and words[0] in _GENERIC_NAMES:
        return False, "generic_name"
    if all(word in _GENERIC_NAMES for word in words):
        return False, "generic_name"
    if (
        source_type == "page.dom.logo_alt"
        and any(word in _THIRD_PARTY_OR_DESCRIPTOR_NAMES for word in words)
    ):
        return False, "third_party_or_descriptor_logo"
    if re.fullmatch(r"(?:www\.)?(?:[a-z0-9-]+\.)+[a-z]{2,}", normalized):
        return False, "domain_not_business_name"
    return True, None


def _valid_address(value: str) -> bool:
    return bool(_ADDRESS_PATTERN.search(" ".join(value.split())))


def _valid_area_name(value: str) -> bool:
    words = value.split()
    return bool(
        1 <= len(words) <= 5
        and re.fullmatch(r"[A-Za-z][A-Za-z .'-]*", value)
        and _normalize_text(value) not in {"service area", "our service area", "areas served"}
    )


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value).strip()).casefold()


def _normalize_name(value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value).strip())


def _excerpt_around(content: str, value: str, radius: int = 90) -> str:
    index = content.find(value)
    if index < 0:
        return value
    return content[max(0, index - radius): index + len(value) + radius]


def _unique_observations(values: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in values:
        if not item:
            continue
        raw = str(item.get("value") or "").strip()
        normalized = str(item.get("normalized_value") or "").strip()
        source_type = str(item.get("source_type") or item.get("source") or "")
        key = (normalized or re.sub(r"\s+", " ", raw).casefold(), source_type, str(item.get("validation") or ""))
        if raw and key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def _observation_values(values: list[dict[str, Any]]) -> list[str]:
    return [str(item["value"]) for item in values if item.get("eligible_for_l3") is True]


def _unique(values: Iterable[Any]) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        key = re.sub(r"\s+", " ", text).casefold()
        if text and key not in seen:
            seen.add(key)
            unique.append(text)
    return unique
