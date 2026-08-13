"""Deterministic backend evaluation for Entity Presence rules 21-25.

These rules answer one bounded question: did the submitted target page expose
the field?  They deliberately do not compare the value with GBP and do not ask
an LLM to reinterpret the page.  Page/GBP equality remains the responsibility
of the Entity Consistency layer.
"""

from __future__ import annotations

import re
from typing import Any, Callable

from app.report_v21.scoring import RULE_FINDING_LABELS


ENTITY_PRESENCE_RULE_IDS: frozenset[int] = frozenset({21, 22, 23, 24, 25})

_STREET_RE = re.compile(
    r"\b\d{1,6}\s+.+?\b(?:street|st\.?|avenue|ave\.?|road|rd\.?|"
    r"boulevard|blvd\.?|drive|dr\.?|lane|ln\.?|court|ct\.?|highway|"
    r"hwy\.?|way|place|pl\.?)\b",
    re.IGNORECASE,
)
_REGION_RE = re.compile(r"(?:^|,\s*)[A-Z]{2}(?:\s+|,|$)")
_POSTAL_RE = re.compile(r"\b\d{5}(?:-\d{4})?\b")


def evaluate_entity_presence_rules(
    page_facts: dict[str, Any],
) -> tuple[dict[int, bool], dict[int, bool], dict[str, Any]]:
    """Return backend-owned results, applicability and auditable findings.

    ``True`` means the absence rule triggered.  All five rules remain
    applicable because the layer reports observable presence; page-type
    context may affect narrative priority later, but it must not change whether
    the submitted page actually exposed the field.
    """
    facts = page_facts if isinstance(page_facts, dict) else {}
    results: dict[int, bool] = {}
    applicability: dict[int, bool] = {}
    findings: dict[str, Any] = {}

    specs: dict[int, tuple[str, str, Callable[[dict[str, Any]], bool]]] = {
        21: ("business_name", "business_names", _has_value),
        22: ("address", "addresses", _has_complete_address),
        23: ("phone", "phones", _has_value),
        24: ("service_area", "service_areas", _has_value),
        25: ("opening_hours", "hours", _has_opening_hours),
    }

    for rule_id, (field, fact_key, detector) in specs.items():
        observations = _page_observations(facts, fact_key)
        values = _values(facts.get(fact_key))
        present = detector({
            "facts": facts,
            "fact_key": fact_key,
            "values": values,
            "observations": observations,
        })
        triggered = not present
        results[rule_id] = triggered
        applicability[rule_id] = True
        findings[f"rule_{rule_id}"] = {
            "finding_key": f"rule_{rule_id}",
            "rule_id": rule_id,
            "affected_layer": "entity_presence",
            "field": field,
            "triggered": triggered,
            "applicable": True,
            "condition": "missing" if triggered else "present",
            "finding": RULE_FINDING_LABELS[rule_id],
            "explanation": (
                f"The checked target page did not expose a valid {field.replace('_', ' ')}."
                if triggered
                else f"The checked target page exposed a valid {field.replace('_', ' ')}."
            ),
            "page_values": values,
            "page_observations": observations,
        }
        if rule_id == 25:
            opening_hours = facts.get("opening_hours")
            if isinstance(opening_hours, dict):
                findings[f"rule_{rule_id}"]["has_conflict"] = bool(
                    opening_hours.get("has_conflict")
                )

    return results, applicability, {
        "schema_version": "1",
        "rule_results": {f"rule_{key}": value for key, value in results.items()},
        "rule_applicability": {
            f"rule_{key}": value for key, value in applicability.items()
        },
        "findings": findings,
    }


def _has_value(payload: dict[str, Any]) -> bool:
    return bool(payload["observations"] or payload["values"])


def _has_complete_address(payload: dict[str, Any]) -> bool:
    """Match Rule 22 from the accepted page-address facts.

    New address observations carry parser-confirmed components.  Those are the
    source of truth for presence.  The text counter remains only for legacy
    reports created before address-fact schema v1.
    """
    for item in payload["observations"]:
        if not isinstance(item, dict):
            continue
        components = item.get("components")
        if not isinstance(components, dict):
            continue
        street = components.get("street") or (
            " ".join(
                str(value or "").strip()
                for value in (
                    components.get("house_number"),
                    components.get("street_name"),
                    components.get("street_suffix"),
                )
                if str(value or "").strip()
            )
        )
        if street and components.get("city") and components.get("state"):
            return True

    candidates = [
        str(item.get("raw_value") or item.get("value") or "").strip()
        for item in payload["observations"]
        if isinstance(item, dict)
    ]
    candidates.extend(payload["values"])
    return any(_address_component_count(value) >= 3 for value in candidates)


def _address_component_count(value: str) -> int:
    text = " ".join(str(value or "").split())
    if not text:
        return 0
    street = bool(_STREET_RE.search(text))
    region = bool(_REGION_RE.search(text))
    postal = bool(_POSTAL_RE.search(text))
    # A locality is the non-empty comma-delimited component immediately before
    # a two-letter region, e.g. "New York" in "..., New York, NY 10002".
    locality = bool(re.search(r",\s*[A-Za-z][A-Za-z .'-]{1,50},\s*[A-Z]{2}\b", text))
    return sum((street, locality, region, postal))


def _has_opening_hours(payload: dict[str, Any]) -> bool:
    opening_hours = payload["facts"].get("opening_hours")
    if isinstance(opening_hours, dict) and opening_hours.get("present") is True:
        return True
    return bool(payload["observations"] or payload["values"])


def _page_observations(page_facts: dict[str, Any], fact_key: str) -> list[dict[str, Any]]:
    observations = page_facts.get("observations")
    raw_items = observations.get(fact_key) if isinstance(observations, dict) else None
    selected: list[dict[str, Any]] = []
    if isinstance(raw_items, list):
        selected = [
            dict(item)
            for item in raw_items
            if (
                isinstance(item, dict)
                and str(item.get("raw_value") or item.get("value") or "").strip()
                and str(item.get("scope") or item.get("source_scope") or "target_page")
                in {"page", "target_page"}
                and str(item.get("validation") or "valid") in {"valid", "unparsed"}
            )
        ]
    if selected:
        return selected

    # Compatibility for stored Page Facts created before the provenance ledger.
    return [
        {
            "value": value,
            "raw_value": value,
            "source": "legacy.checked_page",
            "source_type": "legacy.checked_page",
            "source_label": "Target page · Legacy checked value",
            "scope": "target_page",
            "source_scope": "target_page",
            "validation": "valid",
        }
        for value in _values(page_facts.get(fact_key))
    ]


def _values(value: Any) -> list[str]:
    values = value if isinstance(value, list) else [value]
    result: list[str] = []
    seen: set[str] = set()
    for item in values:
        text = str(item or "").strip()
        key = " ".join(text.casefold().split())
        if text and key not in seen:
            seen.add(key)
            result.append(text)
    return result
