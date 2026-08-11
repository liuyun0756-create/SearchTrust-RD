"""Deterministic, source-preserving page facts used by rules 21-29."""

from __future__ import annotations

import html
import re
from typing import Any, Iterable


_PHONE_PATTERN = re.compile(r"(?<!\d)(?:\+?1[\s.()-]*)?(?:\(?\d{3}\)?[\s.-]*)\d{3}[\s.-]*\d{4}(?!\d)")
_ADDRESS_PATTERN = re.compile(
    r"\b\d{1,6}\s+(?:[NSEW]\.?\s+)?[A-Za-z0-9.' -]{2,60}?\s+"
    r"(?:Street|St\.?|Avenue|Ave\.?|Road|Rd\.?|Boulevard|Blvd\.?|Drive|Dr\.?|"
    r"Lane|Ln\.?|Court|Ct\.?|Highway|Hwy\.?|Way|Place|Pl\.?)(?![A-Za-z])"
    r"(?:\s*,?\s*(?:Suite|Ste\.?|Unit|#)\s*[A-Za-z0-9-]+)?"
    r"(?:\s*,\s*[A-Za-z .'-]{2,40}\s*,\s*[A-Z]{2}\s+\d{5}(?:-\d{4})?)?",
    flags=re.IGNORECASE,
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

_NAME_SOURCE_PRIORITY = {
    "json_ld": 100,
    "visible_brand_heading": 90,
    "logo_alt": 80,
    "og_site_name": 70,
    "title_suffix": 60,
    "title_prefix": 50,
    "copyright_owner": 40,
}
_QUALITY_PRIORITY = {"strong": 30, "supporting": 20, "weak": 10}


def build_page_facts(
    content: str,
    business: Any = None,
    identity_signals: Any = None,
) -> dict[str, Any]:
    """Return raw page observations plus compatibility value arrays.

    Values are never rewritten to resemble GBP.  The L3 evaluator owns all
    field-specific normalization and compares only after these source values
    have been preserved for reporting and evidence.
    """
    text = str(content or "")
    business_record = business if isinstance(business, dict) else {}

    name_observations = _business_name_observations(text, business_record, identity_signals)
    phone_observations = _unique_observations([
        _phone_observation(business_record.get("phone"), "scraper_business"),
        *(
            _observation(match.group(0), "visible_phone", "target_page")
            for match in _PHONE_PATTERN.finditer(text)
        ),
    ])
    address_observations = _unique_observations(
        _observation(match.group(0), "visible_address", "target_page")
        for match in _ADDRESS_PATTERN.finditer(text)
    )
    service_area_observations = _service_area_observations(text)
    hours = _unique(
        match.group(0)
        for pattern in _HOURS_PATTERNS
        for match in pattern.finditer(text)
    )

    observations = {
        "business_names": name_observations,
        "addresses": address_observations,
        "phones": phone_observations,
        "service_areas": service_area_observations,
    }
    return {
        "version": "2",
        "business_names": _observation_values(name_observations),
        "addresses": _observation_values(address_observations),
        "phones": _observation_values(phone_observations),
        "service_areas": _observation_values(service_area_observations),
        "hours": hours,
        "observations": observations,
    }


def _business_name_observations(
    content: str,
    business: dict[str, Any],
    identity_signals: Any,
) -> list[dict[str, str]]:
    business_name = _observation(business.get("name"), "scraper_business", "target_page")
    if business_name:
        return [business_name]

    signal_candidates: list[tuple[int, int, dict[str, str]]] = []
    if isinstance(identity_signals, list):
        for index, signal in enumerate(identity_signals):
            if not isinstance(signal, dict) or str(signal.get("field") or "") != "name":
                continue
            scope = str(signal.get("scope") or "target_page")
            if scope not in {"page", "target_page"}:
                continue
            source = str(signal.get("source") or "identity_signal")
            observation = _observation(signal.get("value"), source, scope)
            if not observation:
                continue
            # Evidence quality dominates source preference: a weak domain-shaped
            # JSON-LD name must never outrank a visible supporting brand value.
            score = (
                _QUALITY_PRIORITY.get(str(signal.get("quality") or ""), 0) * 100
                + _NAME_SOURCE_PRIORITY.get(source, 0)
            )
            signal_candidates.append((score, -index, observation))
    if signal_candidates:
        signal_candidates.sort(reverse=True)
        return [signal_candidates[0][2]]

    match = _SELF_IDENTIFICATION.search(content)
    if match:
        return [_observation(match.group(1), "visible_self_identification", "target_page")]

    match = _FOOTER_OWNER.search(content)
    if match:
        return [_observation(match.group(1), "visible_footer_owner", "target_page")]

    for alt, src in re.findall(r"!\[([^\]\n]{2,100})\]\(([^)]+)\)", content):
        if "logo" not in f"{alt} {src}".casefold():
            continue
        value = re.sub(
            r"\b(?:official|company|business)?\s*logo\b",
            " ",
            html.unescape(alt),
            flags=re.IGNORECASE,
        )
        observation = _observation(value.strip(" -|:_"), "visible_logo_alt", "target_page")
        if observation:
            return [observation]
    return []


def _service_area_observations(content: str) -> list[dict[str, str]]:
    observations: list[dict[str, str]] = []
    for raw_line in content.splitlines():
        # Keep link labels while removing their destinations so navigation such as
        # ``[Service Areas](https://example.com/...)`` cannot become place data.
        line = re.sub(r"\[([^\]]*)\]\([^)]+\)", r"\1", raw_line)
        line = re.sub(r"https?://\S+", " ", line, flags=re.IGNORECASE)
        line = re.sub(r"[*_#`\[\]()]", " ", line)
        match = _SERVICE_PREFIX.search(line)
        if not match:
            continue
        tail = re.split(r"[.!?;|]", match.group(1), maxsplit=1)[0]
        for candidate in re.split(r"\s*(?:,|\band\b|\bor\b|/)\s*", tail, flags=re.IGNORECASE):
            value = re.sub(
                r"^(?:the\s+)?(?:greater\s+)?|\s+(?:area|metro|region|communities|neighborhoods)$",
                "",
                candidate.strip(),
                flags=re.IGNORECASE,
            ).strip(" :-")
            words = value.split()
            if 1 <= len(words) <= 5 and re.fullmatch(r"[A-Za-z][A-Za-z .'-]*", value):
                observations.append(_observation(value, "visible_service_area", "target_page"))
    return _unique_observations(observations)


def _observation(value: Any, source: str, scope: str) -> dict[str, str]:
    text = str(value or "").strip()
    if not text:
        return {}
    return {"value": text, "source": source, "scope": scope}


def _phone_observation(value: Any, source: str) -> dict[str, str]:
    """Keep the observed phone spelling while dropping scraper transport noise."""
    text = str(value or "").strip()
    match = _PHONE_PATTERN.search(text)
    return _observation(match.group(0) if match else "", source, "target_page")


def _unique_observations(values: Iterable[dict[str, str]]) -> list[dict[str, str]]:
    unique: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in values:
        if not item:
            continue
        text = str(item.get("value") or "").strip()
        key = re.sub(r"\s+", " ", text).casefold()
        if text and key not in seen:
            seen.add(key)
            unique.append({
                "value": text,
                "source": str(item.get("source") or "checked_page"),
                "scope": str(item.get("scope") or "target_page"),
            })
    return unique


def _observation_values(values: list[dict[str, str]]) -> list[str]:
    return [item["value"] for item in values]


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
