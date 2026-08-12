"""Deterministic backend evaluation for GBP comparison rules 26-29."""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Callable

from app.report_v21.coverage import build_gbp_status
from app.report_v21.page_facts import normalize_phone
from app.report_v21.scoring import RULE_FINDING_LABELS


GBP_RULE_SPECS: dict[int, tuple[str, str, str, Callable[[str], str], bool]] = {
    26: ("business_name", "business_names", "name", lambda value: _normalize_name(value), True),
    27: ("address", "addresses", "address", lambda value: _normalize_text(value), True),
    28: ("phone", "phones", "phone", lambda value: _normalize_phone(value), True),
    29: ("service_area", "service_areas", "service_areas", lambda value: _normalize_text(value), True),
}


def evaluate_gbp_rules(context: dict[str, Any]) -> tuple[dict[int, bool], dict[int, bool], dict[str, Any]]:
    """Return backend-owned results, applicability and SaaS-safe finding context."""
    page_facts = context.get("page_facts") if isinstance(context.get("page_facts"), dict) else {}
    gbp = context.get("gbp_data") if isinstance(context.get("gbp_data"), dict) else {}
    gbp_status = str(build_gbp_status(context).get("status") or "not_checked")
    gbp_checked = gbp_status == "checked"

    results: dict[int, bool] = {}
    applicability: dict[int, bool] = {}
    findings: dict[str, Any] = {}

    for rule_id, (field, page_key, gbp_key, normalizer, compare_as_set) in GBP_RULE_SPECS.items():
        page_observations = _page_observations(page_facts, page_key)
        page_values = [str(item["value"]) for item in page_observations]
        gbp_values = _values(gbp.get(gbp_key))
        normalized_page = _normalized_values(page_values, normalizer)
        normalized_gbp = _normalized_values(gbp_values, normalizer)
        field_applicable = gbp_checked and not (
            rule_id == 29
            and bool(gbp.get("service_areas_observed"))
            and gbp.get("service_area_business") is False
        )

        if not gbp_checked:
            triggered = False
            condition = "gbp_unavailable"
            explanation = (
                "A checked GBP reference was unavailable, so this comparison was not assessed."
            )
        elif not field_applicable:
            triggered = False
            condition = "field_not_applicable"
            explanation = "The checked GBP identifies a storefront where service-area data is not applicable."
        elif not normalized_page and not normalized_gbp:
            triggered = False
            condition = "both_missing"
            explanation = "Neither checked source exposed this field; its presence is handled by separate rules."
        elif not normalized_page:
            triggered = True
            condition = "page_missing"
            explanation = "GBP exposed this field, but it was not found in the checked page facts."
        elif not normalized_gbp:
            triggered = True
            condition = "gbp_field_missing"
            explanation = "The page exposed this field, but the checked GBP response did not return it."
        else:
            left: Any = set(normalized_page) if compare_as_set else normalized_page
            right: Any = set(normalized_gbp) if compare_as_set else normalized_gbp
            triggered = left != right
            condition = "mismatch" if triggered else "match"
            explanation = (
                "The normalized page and GBP values are not exactly equal."
                if triggered
                else "The normalized page and GBP values are exactly equal."
            )

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
            "finding": RULE_FINDING_LABELS[rule_id],
            "explanation": explanation,
            "page_values": page_values,
            "gbp_values": gbp_values,
            "normalized_page_values": normalized_page,
            "normalized_gbp_values": normalized_gbp,
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
                    "eligible_for_l3": True,
                }
                for value in gbp_values
            ],
            "gbp_status": gbp_status,
        }

    return results, applicability, findings


def _values(value: Any) -> list[str]:
    if isinstance(value, list):
        return [text for item in value if (text := str(item or "").strip())]
    text = str(value or "").strip()
    return [text] if text else []


def _normalized_values(values: list[str], normalizer: Callable[[str], str]) -> list[str]:
    normalized = [normalizer(value) for value in values]
    return list(dict.fromkeys(value for value in normalized if value))


def _page_observations(
    page_facts: dict[str, Any],
    page_key: str,
) -> list[dict[str, Any]]:
    """Return only validated target-page observations eligible for L3.

    Version-2 stored reports did not have eligibility metadata, so their value
    arrays remain a read-only compatibility fallback. New reports cannot enter
    L3 through those arrays when a version-3 observation ledger exists.
    """
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
    if str(page_facts.get("version") or "") == "3":
        # Version 3 is fail-closed: an absent observation list cannot be
        # bypassed by injecting the compatibility value arrays.
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


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value).strip()).casefold()


def _normalize_name(value: str) -> str:
    """Keep every visible business-name difference significant for L3.

    Unicode and whitespace are canonicalized only to avoid invisible transport
    differences.  Case and punctuation remain intact, so values such as
    ``Drain LLC.`` and ``Drain, LLC`` do not silently become a match.
    """
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value).strip())


def _normalize_phone(value: str) -> str:
    normalized, _ = normalize_phone(value)
    return normalized
