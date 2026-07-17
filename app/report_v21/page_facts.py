"""Deterministic extraction of page-side facts used by rules 21-29."""

from __future__ import annotations

import re
from typing import Any


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


def build_page_facts(content: str, business: Any = None) -> dict[str, Any]:
    """Return stable, source-preserving page facts for deterministic comparisons."""
    text = str(content or "")
    business_record = business if isinstance(business, dict) else {}
    names = _unique([business_record.get("name")])
    phones = _unique([business_record.get("phone"), *_PHONE_PATTERN.findall(text)])
    addresses = _unique(match.group(0) for match in _ADDRESS_PATTERN.finditer(text))
    service_areas = _extract_service_areas(text)
    hours = _unique(
        match.group(0)
        for pattern in _HOURS_PATTERNS
        for match in pattern.finditer(text)
    )
    return {
        "version": "1",
        "business_names": names,
        "addresses": addresses,
        "phones": phones,
        "service_areas": service_areas,
        "hours": hours,
    }


def _extract_service_areas(content: str) -> list[str]:
    values: list[str] = []
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
                values.append(value)
    return _unique(values)


def _unique(values: Any) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        key = re.sub(r"\s+", " ", text).casefold()
        if text and key not in seen:
            seen.add(key)
            unique.append(text)
    return unique
