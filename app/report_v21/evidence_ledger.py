"""Backend-owned evidence ledger and rule-to-source binding helpers."""

from __future__ import annotations

import json
import re
from typing import Any

from app.report_v21.scoring import RULE_FINDING_LABELS


MISSING_OBSERVATION_RULES: frozenset[int] = frozenset({
    3, 4, 9, 10, 11, 12,
    21, 22, 23, 24, 25,
    30, 31, 32, 33, 34, 35, 36,
})
GBP_COMPARISON_FIELDS: dict[int, tuple[str, str]] = {
    26: ("business_names", "name"),
    27: ("addresses", "address"),
    28: ("phones", "phone"),
    29: ("service_areas", "service_areas"),
}


def build_evidence_ledger(context: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Create stable evidence IDs from sources already collected by the backend."""
    ledger: dict[str, dict[str, Any]] = {}
    content = str(context.get("content") or context.get("page_content") or "")
    source_url = str(context.get("url") or "") or None

    for index, segment in enumerate(_page_segments(content), start=1):
        evidence_id = f"page-{index:04d}"
        ledger[evidence_id] = {
            "id": evidence_id,
            "source_type": segment["source_type"],
            "source_label": segment["source_label"],
            "source_url": source_url,
            "page_section": segment["page_section"],
            "extracted_text": segment["text"],
        }

    gbp = context.get("gbp_data") if isinstance(context.get("gbp_data"), dict) else {}
    for field in ("name", "address", "phone", "website", "hours", "service_areas", "type"):
        values = _values(gbp.get(field))
        for index, value in enumerate(values, start=1):
            evidence_id = f"gbp-{field.replace('_', '-')}-{index:02d}"
            ledger[evidence_id] = {
                "id": evidence_id,
                "source_type": "gbp",
                "source_label": f"GBP {field.replace('_', ' ')}",
                "source_url": str(context.get("gbp_url") or "") or None,
                "page_section": "Google Business Profile",
                "extracted_text": value,
            }

    reviews = gbp.get("review_list") if isinstance(gbp.get("review_list"), list) else []
    for index, review in enumerate(reviews[:30], start=1):
        if not isinstance(review, dict):
            continue
        text = str(review.get("text") or "").strip()
        if not text:
            continue
        evidence_id = f"review-{index:02d}"
        ledger[evidence_id] = {
            "id": evidence_id,
            "source_type": "review",
            "source_label": f"Recent GBP review {index}",
            "source_url": str(context.get("gbp_url") or "") or None,
            "page_section": "Recent GBP reviews",
            "extracted_text": text,
        }

    schema = context.get("schema_data") if isinstance(context.get("schema_data"), dict) else {}
    for index, schema_type in enumerate(_values(schema.get("types")), start=1):
        evidence_id = f"schema-type-{index:02d}"
        ledger[evidence_id] = {
            "id": evidence_id,
            "source_type": "schema",
            "source_label": "Detected schema type",
            "source_url": str(schema.get("source_url") or source_url or "") or None,
            "page_section": "JSON-LD schema",
            "extracted_text": schema_type,
        }

    return ledger


def serialize_evidence_ledger(ledger: dict[str, dict[str, Any]]) -> str:
    """Serialize only fields the Dify evidence-linker needs."""
    rows = [
        {
            "id": item["id"],
            "source_type": item["source_type"],
            "section": item.get("page_section"),
            "text": item.get("extracted_text"),
        }
        for item in ledger.values()
    ]
    return json.dumps(rows, ensure_ascii=False, separators=(",", ":"))


def build_layer_evidence(
    layer_rule_ids: list[int],
    rule_evidence_ids: dict[int, list[str]],
    ledger: dict[str, dict[str, Any]],
    context: dict[str, Any],
) -> list[dict[str, Any]]:
    """Resolve rule references into immutable backend evidence records."""
    evidence: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()

    for rule_id in layer_rule_ids:
        for evidence_id in rule_evidence_ids.get(rule_id, []):
            item = ledger.get(evidence_id)
            if not item or (evidence_id, rule_id) in seen:
                continue
            seen.add((evidence_id, rule_id))
            evidence.append(_ledger_item_to_evidence(item, rule_id))

        if rule_id in GBP_COMPARISON_FIELDS:
            evidence.extend(_gbp_comparison_evidence(rule_id, context))
        elif rule_id in MISSING_OBSERVATION_RULES and not rule_evidence_ids.get(rule_id):
            evidence.append(_missing_observation_evidence(rule_id, context))

    return _dedupe_evidence(evidence)


def validate_rule_evidence_references(
    rule_results: dict[int, bool],
    references: dict[int, list[str]],
    ledger: dict[str, dict[str, Any]],
) -> list[str]:
    """Return errors for invented IDs or unsupported positive observations."""
    errors: list[str] = []
    active_ids = set(rule_results)
    for rule_id, evidence_ids in references.items():
        if rule_id not in active_ids:
            errors.append(f"rule_{rule_id} is not an active rule.")
            continue
        if not rule_results.get(rule_id) and evidence_ids:
            errors.append(f"rule_{rule_id} cannot reference evidence when the rule is false.")
        for evidence_id in evidence_ids:
            if evidence_id not in ledger:
                errors.append(f"rule_{rule_id} referenced unknown evidence ID {evidence_id}.")

    self_sufficient = MISSING_OBSERVATION_RULES | frozenset(GBP_COMPARISON_FIELDS)
    for rule_id, triggered in rule_results.items():
        if triggered and rule_id not in self_sufficient and not references.get(rule_id):
            errors.append(f"rule_{rule_id} requires at least one backend evidence ID.")
    return errors


def _page_segments(content: str) -> list[dict[str, str]]:
    segments: list[dict[str, str]] = []
    source_type = "page"
    source_label = "Checked page content"
    page_section = "Main page"

    for raw in content.splitlines():
        line = raw.strip()
        if not line:
            continue
        marker = re.match(r"^===\s*(.+?)\s*===$", line)
        if marker:
            marker_text = marker.group(1).strip().lower()
            if marker_text.startswith("end "):
                source_type, source_label, page_section = "page", "Checked page content", "Main page"
                continue
            page_section = marker.group(1).strip()
            if "contact" in marker_text:
                source_type, source_label = "contact_page", "Checked contact page"
            elif "about" in marker_text or "our-story" in marker_text or "who-we-are" in marker_text:
                source_type, source_label = "about_page", "Checked about page"
            else:
                source_type, source_label = "site_internal", "Checked internal page"
            continue

        text = re.sub(r"\s+", " ", line).strip()
        if len(text) < 12:
            continue
        segments.append({
            "source_type": source_type,
            "source_label": source_label,
            "page_section": page_section,
            "text": text[:700],
        })
        if len(segments) >= 240:
            break
    return segments


def _ledger_item_to_evidence(item: dict[str, Any], rule_id: int) -> dict[str, Any]:
    return {
        "id": f"ev-rule-{rule_id}-{item['id']}",
        "source_type": item["source_type"],
        "source_label": item["source_label"],
        "source_url": item.get("source_url"),
        "page_section": item.get("page_section"),
        "extracted_text": item.get("extracted_text"),
        "normalized_value": None,
        "expected_value": None,
        "comparison_result": "partial",
        "confidence": "high",
        "explanation": RULE_FINDING_LABELS.get(rule_id, f"Supports triggered rule {rule_id}."),
    }


def _missing_observation_evidence(rule_id: int, context: dict[str, Any]) -> dict[str, Any]:
    sub_pages = context.get("sub_pages") if isinstance(context.get("sub_pages"), list) else []
    scope = "Main page"
    if sub_pages:
        scope = f"Main page and {len(sub_pages)} successfully checked internal page(s)"
    finding = RULE_FINDING_LABELS.get(rule_id, f"Rule {rule_id} condition was not found.")
    return {
        "id": f"ev-rule-{rule_id}-missing",
        "source_type": "page",
        "source_label": "Checked page scope",
        "source_url": str(context.get("url") or "") or None,
        "page_section": scope,
        "extracted_text": None,
        "normalized_value": f"Not found in the checked scope: {finding}",
        "expected_value": finding,
        "comparison_result": "missing",
        "confidence": "high",
        "explanation": finding,
    }


def _gbp_comparison_evidence(rule_id: int, context: dict[str, Any]) -> list[dict[str, Any]]:
    page_field, gbp_field = GBP_COMPARISON_FIELDS[rule_id]
    page_facts = context.get("page_facts") if isinstance(context.get("page_facts"), dict) else {}
    gbp = context.get("gbp_data") if isinstance(context.get("gbp_data"), dict) else {}
    page_values = _values(page_facts.get(page_field))
    gbp_values = _values(gbp.get(gbp_field))
    field_label = gbp_field.replace("_", " ")
    finding = RULE_FINDING_LABELS[rule_id]
    items: list[dict[str, Any]] = []

    if page_values:
        for index, value in enumerate(page_values[:4], start=1):
            items.append({
                "id": f"ev-rule-{rule_id}-page-{index:02d}",
                "source_type": "page",
                "source_label": f"Page {field_label}",
                "source_url": str(context.get("url") or "") or None,
                "page_section": "Structured page-to-GBP comparison",
                "extracted_text": value,
                "normalized_value": None,
                "expected_value": "; ".join(gbp_values) or "GBP field not available",
                "comparison_result": "mismatch",
                "confidence": "high",
                "explanation": finding,
            })
    else:
        items.append({
            "id": f"ev-rule-{rule_id}-page-missing",
            "source_type": "page",
            "source_label": f"Page {field_label}",
            "source_url": str(context.get("url") or "") or None,
            "page_section": "Structured page-to-GBP comparison",
            "extracted_text": None,
            "normalized_value": "Not found in the checked page scope",
            "expected_value": "; ".join(gbp_values) or "GBP field not available",
            "comparison_result": "missing",
            "confidence": "high",
            "explanation": finding,
        })

    for index, value in enumerate(gbp_values[:4], start=1):
        items.append({
            "id": f"ev-rule-{rule_id}-gbp-{index:02d}",
            "source_type": "gbp",
            "source_label": f"GBP {field_label}",
            "source_url": str(context.get("gbp_url") or "") or None,
            "page_section": "Google Business Profile",
            "extracted_text": value,
            "normalized_value": None,
            "expected_value": "; ".join(page_values) or "Page field not found",
            "comparison_result": "mismatch",
            "confidence": "high",
            "explanation": finding,
        })
    return items


def _values(value: Any) -> list[str]:
    if isinstance(value, list):
        return [text for item in value if (text := str(item).strip())]
    if value is None:
        return []
    text = str(value).strip()
    return [text] if text else []


def _dedupe_evidence(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in items:
        key = (
            str(item.get("source_type")),
            str(item.get("extracted_text") or item.get("normalized_value")),
            str(item.get("comparison_result")),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result
