"""Provenance-preserving opening-hours facts for page and GBP observations.

This module does not decide the scored L2/L3 rule vector.  It preserves raw
values, classifies their meaning and location, and produces a shared weekly
shape used by the non-scoring GBP x Page hours audit.
"""

from __future__ import annotations

import html
import json
import re
from datetime import datetime, timezone
from typing import Any, Iterable


DAY_ORDER = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)
_DAY_INDEX = {day: index for index, day in enumerate(DAY_ORDER)}
_DAY_ALIASES = {
    "mon": "monday",
    "monday": "monday",
    "tue": "tuesday",
    "tues": "tuesday",
    "tuesday": "tuesday",
    "wed": "wednesday",
    "wednesday": "wednesday",
    "thu": "thursday",
    "thur": "thursday",
    "thurs": "thursday",
    "thursday": "thursday",
    "fri": "friday",
    "friday": "friday",
    "sat": "saturday",
    "saturday": "saturday",
    "sun": "sunday",
    "sunday": "sunday",
}
_DAY_TOKEN = (
    r"mon(?:day)?|tue(?:s|sday)?|wed(?:nesday)?|"
    r"thu(?:r|rs|rsday)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?"
)
_DAY_RANGE_RE = re.compile(
    rf"\b(?P<start>{_DAY_TOKEN})\b\s*(?:-|–|—|to|through|thru)\s*"
    rf"\b(?P<end>{_DAY_TOKEN})\b",
    re.IGNORECASE,
)
_DAY_SINGLE_RE = re.compile(rf"\b({_DAY_TOKEN})\b", re.IGNORECASE)
_TIME_RANGE_RE = re.compile(
    r"(?P<start_hour>\d{1,2})(?::(?P<start_minute>\d{2}))?\s*"
    r"(?P<start_ampm>a\.?m\.?|p\.?m\.?)?\s*"
    r"(?:-|–|—|to)\s*"
    r"(?P<end_hour>\d{1,2})(?::(?P<end_minute>\d{2}))?\s*"
    r"(?P<end_ampm>a\.?m\.?|p\.?m\.?)?",
    re.IGNORECASE,
)
_TIME_24_RANGE_RE = re.compile(
    r"(?P<start>(?:[01]?\d|2[0-3]):[0-5]\d)\s*(?:-|–|—|to)\s*"
    r"(?P<end>(?:[01]?\d|2[0-3]):[0-5]\d)",
    re.IGNORECASE,
)
_HOURS_SIGNAL_RE = re.compile(
    rf"(?:\b24\s*/\s*7\b|\b(?:open\s+)?24[ -]?hours?\b|"
    rf"\b(?:{_DAY_TOKEN})\b[^\n]{{0,120}}(?:closed|"
    rf"\d{{1,2}}(?::\d{{2}})?\s*(?:a\.?m\.?|p\.?m\.?)))",
    re.IGNORECASE,
)
_JSON_LD_RE = re.compile(
    r"<script\b[^>]*type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script\s*>",
    re.IGNORECASE | re.DOTALL,
)
_SCRIPT_STYLE_RE = re.compile(
    r"<(?:script|style|noscript|template|svg)\b[^>]*>.*?</(?:script|style|noscript|template|svg)\s*>",
    re.IGNORECASE | re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>", re.DOTALL)
_CONTAINER_PATTERNS = (
    ("footer", re.compile(r"<footer\b[^>]*>(.*?)</footer\s*>", re.IGNORECASE | re.DOTALL)),
    ("header", re.compile(r"<header\b[^>]*>(.*?)</header\s*>", re.IGNORECASE | re.DOTALL)),
)


def extract_page_hours_facts(
    content: str,
    structured_content: str = "",
    *,
    source_url: str = "",
) -> dict[str, Any]:
    """Return every bounded page-hours observation with provenance metadata."""
    readable = _visible_text(content)
    structured = str(structured_content or "")
    observations: list[dict[str, Any]] = []

    for record in _jsonld_records(structured):
        for raw in _jsonld_opening_hours(record):
            observations.append(_page_observation(
                raw,
                source_url=source_url,
                source_type="page.jsonld.opening_hours",
                source_location="json_ld",
                semantic_role="regular_business_hours",
                locator="script[type='application/ld+json']",
            ))

    structured_matches: set[str] = set()
    for source_location, pattern in _CONTAINER_PATTERNS:
        for container in pattern.findall(structured):
            for line in _hours_lines(_visible_text(container)):
                observations.append(_page_observation(
                    line,
                    source_url=source_url,
                    source_type="page.dom.visible_hours",
                    source_location=source_location,
                    semantic_role=_semantic_role(line),
                    locator=source_location,
                ))
                structured_matches.add(_fact_key(line))

    for index, line in enumerate(_hours_lines(readable)):
        if _fact_key(line) in structured_matches:
            continue
        observations.append(_page_observation(
            line,
            source_url=source_url,
            source_type="page.dom.visible_hours",
            source_location=_infer_location(line, readable),
            semantic_role=_semantic_role(line),
            locator=f"visible hours line {index + 1}",
        ))

    observations = _unique_observations(observations)
    by_role: dict[str, list[dict[str, Any]]] = {}
    for item in observations:
        by_role.setdefault(str(item["semantic_role"]), []).append(item)

    conflicts = {
        role: _role_has_conflict(items)
        for role, items in by_role.items()
    }
    return {
        "present": bool(observations),
        "observations": observations,
        "by_semantic_role": by_role,
        "has_conflict": any(conflicts.values()),
        "conflicts_by_semantic_role": conflicts,
    }


def build_gbp_hours_facts(gbp_data: dict[str, Any]) -> dict[str, Any]:
    """Separate GBP's current-open summary from its complete weekly schedule."""
    if not isinstance(gbp_data, dict):
        gbp_data = {}
    summary = gbp_data.get("hours_summary")
    if summary in (None, ""):
        summary = gbp_data.get("open_state")
    if summary in (None, "") and isinstance(gbp_data.get("hours"), str):
        summary = gbp_data.get("hours")

    weekly_raw = gbp_data.get("operating_hours_raw")
    if weekly_raw in (None, "", [], {}):
        candidate = gbp_data.get("operating_hours")
        if candidate not in (None, "", [], {}):
            weekly_raw = candidate
    if weekly_raw in (None, "", [], {}) and isinstance(gbp_data.get("hours"), (dict, list)):
        weekly_raw = gbp_data.get("hours")

    normalized = normalize_weekly_hours(weekly_raw)
    return {
        "summary_raw": _clean_text(summary) or None,
        "weekly_raw": weekly_raw if weekly_raw not in (None, "", [], {}) else None,
        "weekly_display": format_weekly_hours(weekly_raw) or None,
        "normalized_schedule": normalized,
        "complete": set(normalized) == set(DAY_ORDER),
        "source_type": "serpapi_google_maps",
        "data_id": _clean_text(gbp_data.get("data_id")) or None,
    }


def build_source_facts(
    page_facts: dict[str, Any],
    gbp_data: dict[str, Any],
    *,
    source_url: str,
    fetched_at: str | None = None,
) -> dict[str, Any]:
    """Build the safe, versioned fact snapshot persisted with a report."""
    page_hours = page_facts.get("opening_hours") if isinstance(page_facts, dict) else None
    if not isinstance(page_hours, dict):
        page_hours = {
            "present": False,
            "observations": [],
            "by_semantic_role": {},
            "has_conflict": False,
            "conflicts_by_semantic_role": {},
        }
    gbp_hours = build_gbp_hours_facts(gbp_data)
    gbp_hours["fetched_at"] = fetched_at or datetime.now(timezone.utc).isoformat()
    return {
        "schema_version": "1",
        "page": {
            "source_url": source_url,
            "opening_hours": page_hours,
        },
        "gbp": {
            "opening_hours": gbp_hours,
        },
    }


def compare_page_gbp_hours(source_facts: dict[str, Any]) -> dict[str, Any]:
    """Strictly compare Page facts with GBP's complete weekly schedule.

    GBP's current-open summary is deliberately excluded.  A match requires
    every Monday-Sunday GBP day to have an exactly equal Page observation for
    that day.  Page observations may come from different visible/structured
    sources; internal Page conflicts are retained as evidence but do not, by
    themselves, decide the comparison result.
    """
    facts = source_facts if isinstance(source_facts, dict) else {}
    page = facts.get("page") if isinstance(facts.get("page"), dict) else {}
    page_hours = (
        page.get("opening_hours")
        if isinstance(page.get("opening_hours"), dict)
        else {}
    )
    observations = (
        page_hours.get("observations")
        if isinstance(page_hours.get("observations"), list)
        else []
    )

    gbp = facts.get("gbp") if isinstance(facts.get("gbp"), dict) else {}
    gbp_hours = (
        gbp.get("opening_hours")
        if isinstance(gbp.get("opening_hours"), dict)
        else {}
    )
    gbp_schedule = gbp_hours.get("normalized_schedule")
    if not isinstance(gbp_schedule, dict):
        gbp_schedule = {}
    gbp_complete = bool(gbp_hours.get("complete")) and set(gbp_schedule) == set(DAY_ORDER)
    if not gbp_complete:
        return {
            "status": "not_checked",
            "explanation": (
                "The GBP response did not return a complete Monday-Sunday schedule; "
                "its current-hours summary was not used for comparison."
            ),
            "matched_days": [],
            "unmatched_days": [],
        }

    page_day_values: dict[str, set[str]] = {day: set() for day in DAY_ORDER}
    for observation in observations:
        if not isinstance(observation, dict):
            continue
        if observation.get("eligible_for_l3") is False:
            continue
        schedule = observation.get("normalized_schedule")
        if not isinstance(schedule, dict):
            continue
        for day, day_value in schedule.items():
            if day in page_day_values and isinstance(day_value, dict):
                page_day_values[day].add(_canonical_day_value(day_value))

    if not any(page_day_values.values()):
        return {
            "status": "missing",
            "explanation": (
                "GBP returned a complete weekly schedule, but the checked page did not "
                "expose a comparable hours fact."
            ),
            "matched_days": [],
            "unmatched_days": list(DAY_ORDER),
        }

    matched_days = [
        day
        for day in DAY_ORDER
        if _canonical_day_value(gbp_schedule[day]) in page_day_values[day]
    ]
    unmatched_days = [day for day in DAY_ORDER if day not in matched_days]
    if not unmatched_days:
        return {
            "status": "match",
            "explanation": (
                "The page and GBP complete weekly schedules are exactly equal for "
                "every day from Monday through Sunday."
            ),
            "matched_days": matched_days,
            "unmatched_days": [],
        }
    return {
        "status": "mismatch",
        "explanation": (
            "The page and GBP complete weekly schedules are not exactly equal for: "
            f"{', '.join(day.title() for day in unmatched_days)}."
        ),
        "matched_days": matched_days,
        "unmatched_days": unmatched_days,
    }


def normalize_weekly_hours(value: Any) -> dict[str, dict[str, Any]]:
    """Normalize common Page/SerpAPI hours shapes into a shared weekly map."""
    result: dict[str, dict[str, Any]] = {}
    for raw in _hours_values(value):
        schedule = normalize_hours_text(raw)
        for day, day_value in schedule.items():
            existing = result.get(day)
            if existing is None:
                result[day] = day_value
            elif existing != day_value:
                intervals = list(existing.get("intervals") or [])
                for interval in day_value.get("intervals") or []:
                    if interval not in intervals:
                        intervals.append(interval)
                result[day] = {
                    "status": "open" if intervals else day_value.get("status", "unknown"),
                    "intervals": intervals,
                }
    return {day: result[day] for day in DAY_ORDER if day in result}


def normalize_hours_text(value: Any) -> dict[str, dict[str, Any]]:
    text = _clean_text(value)
    if not text:
        return {}
    lowered = text.casefold()
    if re.search(r"\b24\s*/\s*7\b", lowered):
        return {day: _open_24_hours() for day in DAY_ORDER}

    days = _days_from_text(text)
    if not days:
        return {}
    if re.search(r"\bclosed\b", lowered):
        return {day: {"status": "closed", "intervals": []} for day in days}
    if re.search(r"\b(?:open\s+)?24[ -]?hours?\b", lowered):
        return {day: _open_24_hours() for day in days}

    intervals: list[list[str]] = []
    for match in _TIME_RANGE_RE.finditer(text):
        start_ampm = match.group("start_ampm") or match.group("end_ampm")
        end_ampm = match.group("end_ampm") or match.group("start_ampm")
        if not start_ampm or not end_ampm:
            continue
        start = _clock_time(
            f"{match.group('start_hour')}:{match.group('start_minute') or '00'} {start_ampm}"
        )
        end = _clock_time(
            f"{match.group('end_hour')}:{match.group('end_minute') or '00'} {end_ampm}"
        )
        if start and end:
            if start == end == "00:00":
                end = "24:00"
            interval = [start, end]
            if interval not in intervals:
                intervals.append(interval)
    if not intervals:
        for match in _TIME_24_RANGE_RE.finditer(text):
            start = match.group("start").zfill(5)
            end = match.group("end").zfill(5)
            if start == end == "00:00":
                end = "24:00"
            intervals.append([start, end])
    if intervals:
        return {day: {"status": "open", "intervals": intervals} for day in days}
    return {day: {"status": "unknown", "intervals": []} for day in days}


def format_weekly_hours(value: Any) -> str:
    """Render provider-returned weekly hours without replacing them with a summary."""
    entries: list[str] = []
    if isinstance(value, dict):
        for day in DAY_ORDER:
            raw = next((item for key, item in value.items() if _day_name(key) == day), None)
            if raw not in (None, ""):
                entries.append(f"{day.title()}: {_display_value(raw)}")
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                for key, raw in item.items():
                    day = _day_name(key)
                    entries.append(f"{(day or str(key)).title()}: {_display_value(raw)}")
            elif _clean_text(item):
                entries.append(_clean_text(item))
    elif _clean_text(value):
        entries.append(_clean_text(value))
    return "; ".join(dict.fromkeys(entries))


def format_page_hours_observations(value: Any) -> str:
    observations = value if isinstance(value, list) else []
    lines = []
    for item in observations:
        if not isinstance(item, dict):
            continue
        raw = _clean_text(item.get("raw_value") or item.get("value"))
        if not raw:
            continue
        location = str(item.get("source_location") or "page").replace("_", " ").title()
        role = str(item.get("semantic_role") or "unknown availability").replace("_", " ").title()
        lines.append(f"{raw} [{location} · {role}]")
    return "\n".join(dict.fromkeys(lines))


def _page_observation(
    raw: str,
    *,
    source_url: str,
    source_type: str,
    source_location: str,
    semantic_role: str,
    locator: str,
) -> dict[str, Any]:
    normalized = normalize_hours_text(raw)
    return {
        "value": _clean_text(raw),
        "raw_value": _clean_text(raw),
        "normalized_schedule": normalized,
        "source": source_type,
        "source_type": source_type,
        "source_label": f"Target page · {source_location.replace('_', ' ').title()}",
        "source_system": "page",
        "source_location": source_location,
        "semantic_role": semantic_role,
        "scope": "target_page",
        "source_scope": "target_page",
        "source_url": source_url or None,
        "locator": locator,
        "visibility": "structured" if source_location == "json_ld" else "visible",
        "validation": "valid" if normalized else "unparsed",
        "eligible_for_l3": bool(normalized),
    }


def _hours_lines(value: str) -> list[str]:
    lines: list[str] = []
    raw_lines = [_clean_text(line) for line in str(value or "").splitlines()]
    for index, line in enumerate(raw_lines):
        if not line or not _HOURS_SIGNAL_RE.search(line):
            continue
        lines.append(line[:300])
        if index + 1 < len(raw_lines):
            following = raw_lines[index + 1]
            if following and _DAY_SINGLE_RE.search(following) and _HOURS_SIGNAL_RE.search(following):
                lines.append(following[:300])
    return list(dict.fromkeys(lines))


def _semantic_role(value: str) -> str:
    lowered = value.casefold()
    if "emergency" in lowered:
        return "emergency_availability"
    if "appointment" in lowered or "booking" in lowered:
        return "appointment_hours"
    return "regular_business_hours"


def _infer_location(line: str, content: str) -> str:
    index = content.find(line)
    if index < 0 or not content:
        return "body"
    ratio = index / max(len(content), 1)
    if ratio <= 0.18:
        return "header_banner"
    if ratio >= 0.78:
        return "footer"
    return "body"


def _role_has_conflict(items: list[dict[str, Any]]) -> bool:
    observed_days: dict[str, str] = {}
    for item in items:
        schedule = item.get("normalized_schedule")
        if not isinstance(schedule, dict):
            continue
        for day, day_value in schedule.items():
            serialized = json.dumps(day_value, sort_keys=True)
            if day in observed_days and observed_days[day] != serialized:
                return True
            observed_days[day] = serialized
    return False


def _hours_values(value: Any) -> Iterable[str]:
    if isinstance(value, dict):
        for key, raw in value.items():
            day = _day_name(key)
            if isinstance(raw, list):
                for item in raw:
                    yield f"{day or key}: {_clean_text(item)}"
            else:
                yield f"{day or key}: {_clean_text(raw)}"
    elif isinstance(value, list):
        for item in value:
            yield from _hours_values(item)
    elif _clean_text(value):
        yield _clean_text(value)


def _days_from_text(value: str) -> list[str]:
    range_match = _DAY_RANGE_RE.search(value)
    if range_match:
        start = _day_name(range_match.group("start"))
        end = _day_name(range_match.group("end"))
        if start and end:
            start_index = _DAY_INDEX[start]
            end_index = _DAY_INDEX[end]
            if start_index <= end_index:
                return list(DAY_ORDER[start_index:end_index + 1])
            return list(DAY_ORDER[start_index:]) + list(DAY_ORDER[:end_index + 1])
    return list(dict.fromkeys(
        day for match in _DAY_SINGLE_RE.finditer(value)
        if (day := _day_name(match.group(1)))
    ))


def _day_name(value: Any) -> str:
    token = re.sub(r"[^a-z]", "", str(value or "").casefold())
    if token.startswith("http"):
        return ""
    return _DAY_ALIASES.get(token, "")


def _clock_time(value: str) -> str:
    match = re.search(r"(\d{1,2})(?::(\d{2}))?\s*([ap])", value.casefold())
    if not match:
        return ""
    hour = int(match.group(1)) % 12
    minute = int(match.group(2) or "0")
    if match.group(3) == "p":
        hour += 12
    if minute > 59:
        return ""
    return f"{hour:02d}:{minute:02d}"


def _open_24_hours() -> dict[str, Any]:
    return {"status": "open", "intervals": [["00:00", "24:00"]]}


def _canonical_day_value(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    status = str(value.get("status") or "unknown").casefold()
    intervals = sorted(
        [str(part) for part in interval]
        for interval in value.get("intervals") or []
        if isinstance(interval, list) and len(interval) == 2
    )
    return json.dumps(
        {"status": status, "intervals": intervals},
        sort_keys=True,
        separators=(",", ":"),
    )


def _jsonld_records(value: str) -> Iterable[dict[str, Any]]:
    for raw in _JSON_LD_RE.findall(value or ""):
        try:
            parsed = json.loads(html.unescape(raw).strip())
        except (json.JSONDecodeError, TypeError):
            continue
        yield from _schema_records(parsed)


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


def _jsonld_opening_hours(record: dict[str, Any]) -> list[str]:
    values: list[str] = []
    raw_hours = record.get("openingHours")
    for item in raw_hours if isinstance(raw_hours, list) else [raw_hours]:
        if _clean_text(item):
            values.append(_clean_text(item))
    specifications = record.get("openingHoursSpecification")
    for item in specifications if isinstance(specifications, list) else [specifications]:
        if not isinstance(item, dict):
            continue
        raw_days = item.get("dayOfWeek")
        days = raw_days if isinstance(raw_days, list) else [raw_days]
        day_text = ", ".join(_clean_text(day).split("/")[-1] for day in days if _clean_text(day))
        opens = _clean_text(item.get("opens"))
        closes = _clean_text(item.get("closes"))
        if day_text and opens and closes:
            values.append(f"{day_text}: {opens} - {closes}")
    return values


def _visible_text(value: str) -> str:
    text = _SCRIPT_STYLE_RE.sub("\n", str(value or ""))
    # Block boundaries delimit evidence lines; inline markup such as <strong>
    # must not split "Mon-Sat" from the adjacent time range.
    text = re.sub(
        r"</?(?:address|article|aside|br|div|footer|header|li|main|nav|p|section|tr|h[1-6])\b[^>]*>",
        "\n",
        text,
        flags=re.IGNORECASE,
    )
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text).replace("\xa0", " ")
    return "\n".join(_clean_text(line) for line in text.splitlines() if _clean_text(line))


def _unique_observations(values: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in values:
        raw = _fact_key(item.get("raw_value"))
        key = (raw, str(item.get("source_location") or ""), str(item.get("semantic_role") or ""))
        if raw and key not in seen:
            seen.add(key)
            result.append(item)
    return result


def _display_value(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(_clean_text(item) for item in value if _clean_text(item))
    return _clean_text(value)


def _fact_key(value: Any) -> str:
    return re.sub(r"\s+", " ", _clean_text(value)).casefold()


def _clean_text(value: Any) -> str:
    return " ".join(html.unescape(str(value or "")).split()).strip()
