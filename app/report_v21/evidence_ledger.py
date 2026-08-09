"""Backend-owned evidence ledger and rule-to-source binding helpers."""

from __future__ import annotations

import json
import re
from typing import Any

from app.report_v21.scoring import RULE_FINDING_LABELS


MISSING_OBSERVATION_RULES: frozenset[int] = frozenset({
    3, 4, 6, 7, 8, 9, 10, 11, 12, 14,
    21, 22, 23, 24, 25,
    30, 31, 32, 33, 34, 35, 36,
})
GBP_COMPARISON_FIELDS: dict[int, tuple[str, str]] = {
    26: ("business_names", "name"),
    27: ("addresses", "address"),
    28: ("phones", "phone"),
    29: ("service_areas", "service_areas"),
}
PAGE_SOURCE_TYPES: frozenset[str] = frozenset({
    "page", "schema", "contact_page", "about_page", "site_internal",
})
RULE_EVIDENCE_TERMS: dict[int, tuple[str, ...]] = {
    1: ("city", "town", "area", "county", "near", "serving"),
    2: ("service", "repair", "install", "replacement", "emergency"),
    6: ("image", "photo", "gallery", "before and after"),
    13: ("service", "location", "area", "city"),
    15: ("review", "rating", "award", "years", "trusted"),
    16: ("service", "repair", "emergency", "near me", "city"),
    17: ("company", "business", "service", "plumbing", "contractor"),
    18: ("service", "repair", "install", "category", "specialist"),
    19: ("call", "phone", "contact", "company", "business"),
    20: ("service", "repair", "contractor", "company", "local"),
}

MISSING_EVIDENCE_STRATEGIES: dict[int, dict[str, Any]] = {
    3: {"label": "Geographic context", "terms": ("serving", "service area", "community", "city", "county", "near")},
    4: {"label": "Time and activity context", "kinds": ("text", "cta"), "terms": ("24/7", "24 hours", "same day", "today", "recent", "since", "year")},
    6: {"label": "Page imagery", "kinds": ("image",)},
    7: {"label": "Service process", "terms": ("we", "our team", "technician", "diagnose", "inspect", "repair", "install")},
    8: {"label": "Calls to action", "kinds": ("cta",), "terms": ("call", "book", "schedule", "quote", "estimate", "contact", "request")},
    9: {"label": "Service responsibility and follow-up", "terms": ("technician", "our team", "we", "assess", "quote", "repair", "responsible", "warranty", "guarantee", "follow up")},
    10: {"label": "Service responsibility and follow-up", "terms": ("diagnose", "assess", "quote", "repair", "parts", "limitations", "warranty", "follow up", "return visit")},
    11: {"label": "Verifiable trust signals", "terms": ("license", "licensed", "insured", "certified", "award", "association", "warranty")},
    12: {"label": "Service responsibility and follow-up", "terms": ("assess", "quote", "repair", "warranty", "guarantee", "follow up", "correction", "our team", "technician")},
    14: {"label": "Page purpose and value", "terms": ("service", "repair", "install", "why choose", "benefit")},
    21: {"label": "Business identity", "terms": ("company", "business", "plumbing", "contractor", "contact")},
    22: {"label": "Address", "terms": ("address", "location", "contact", "street", "road", "avenue")},
    23: {"label": "Phone", "kinds": ("cta", "text"), "terms": ("call", "phone", "tel", "contact")},
    24: {"label": "Service area", "terms": ("serving", "service area", "areas we serve", "community", "city")},
    25: {"label": "Business hours", "kinds": ("text", "cta"), "terms": ("hours", "open", "24/7", "monday", "saturday", "sunday")},
    30: {"label": "Geographic context", "terms": ("serving", "service area", "community", "neighborhood", "city", "county")},
    31: {"label": "Geographic context", "terms": ("serving", "service area", "community", "neighborhood", "near", "city")},
    32: {"label": "Local context", "terms": ("local", "serving", "service area", "community", "neighborhood", "city")},
    33: {"label": "Service boundary", "terms": ("serving", "service area", "miles", "radius", "surrounding", "nearby", "coverage")},
    34: {"label": "Service examples", "terms": ("repair", "install", "job", "project", "customer", "homeowner", "technician")},
    35: {"label": "Customer context", "terms": ("customer", "homeowner", "business owner", "property", "home", "commercial")},
    36: {"label": "Time and activity context", "terms": ("same day", "today", "recent", "last", "since", "year", "hour", "minute")},
}

SHORT_MISSING_LABELS: dict[int, str] = {
    3: "No geographic or real-world anchor found",
    4: "No dated activity or service timeline found",
    6: "No clearly original page imagery identified",
    7: "No concrete first-person work detail found",
    8: "No page-specific call to action found",
    9: "No operational responsibility statement found",
    10: "No limits or complex-case guidance found",
    11: "No externally verifiable trust clue found",
    12: "No outcome or follow-up responsibility stated",
    14: "No distinct standalone page value found",
    21: "No clear business identity found",
    22: "No street address found",
    23: "No contact phone number found",
    24: "No clear service-area statement found",
    25: "No business hours found",
    30: "No community-level location detail found",
    31: "No concrete landmark reference found",
    32: "No factual local operating context found",
    33: "No service radius or operating boundary stated",
    34: "No specific service case found",
    35: "No customer situation described",
    36: "No meaningful time context found",
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
            "evidence_kind": segment["kind"],
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

    review_corpus = context.get("review_corpus")
    reviews = review_corpus if isinstance(review_corpus, list) else []
    for index, review in enumerate(reviews, start=1):
        if not isinstance(review, dict):
            continue
        text = str(review.get("text") or "").strip()
        if not text:
            continue
        evidence_id = str(review.get("id") or f"review-{index:02d}")
        ledger[evidence_id] = {
            "id": evidence_id,
            "source_type": "review",
            "source_label": str(review.get("source_label") or f"Checked review {index}"),
            "source_url": str(review.get("source_url") or "") or None,
            "page_section": str(review.get("section") or "Checked review corpus"),
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


def build_layer_evidence(
    layer_rule_ids: list[int],
    ledger: dict[str, dict[str, Any]],
    context: dict[str, Any],
) -> list[dict[str, Any]]:
    """Build evidence from backend facts without Dify evidence references."""
    evidence: list[dict[str, Any]] = []

    for rule_id in layer_rule_ids:
        if rule_id in GBP_COMPARISON_FIELDS:
            evidence.extend(_gbp_comparison_evidence(rule_id, context))
        elif rule_id in {37, 38, 39}:
            evidence.extend(_review_rule_evidence(rule_id, ledger, context))
        elif rule_id in MISSING_OBSERVATION_RULES:
            evidence.append(_missing_observation_evidence(rule_id, ledger, context))
        else:
            evidence.append(_page_rule_evidence(rule_id, ledger, context))

    return _dedupe_evidence(evidence)


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

        readable = _readable_page_line(line)
        if not readable:
            continue
        text, kind = readable
        if len(text) < 12 and kind not in {"cta", "image"}:
            continue
        segments.append({
            "source_type": source_type,
            "source_label": source_label,
            "page_section": page_section,
            "text": text[:360],
            "kind": kind,
        })
        if len(segments) >= 240:
            break
    return segments


def _readable_page_line(line: str) -> tuple[str, str] | None:
    """Return user-readable page text without asset URLs or encoded SVG data."""
    image_alts = [
        re.sub(r"\s+", " ", alt).strip(" -_#")
        for alt in re.findall(r"!\[([^\]]*)\]\([^)]+\)", line)
        if _meaningful_image_alt(re.sub(r"\s+", " ", alt).strip(" -_#"))
    ]
    without_images = re.sub(r"!\[[^\]]*\]\([^)]+\)", " ", line)
    image_remainder = re.sub(r"\[[^\]]*\]\([^)]+\)", " ", without_images)
    if image_alts and not re.sub(r"[#*`\[\]()\s-]", "", image_remainder):
        return "; ".join(image_alts[:3])[:360], "image"

    link_targets = re.findall(r"\[([^\]]+)\]\(([^)]+)\)", without_images)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", without_images)
    text = re.sub(r"https?://\S+", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"^#{1,6}\s*", "", text)
    text = re.sub(r"\s+", " ", text).strip(" -*#|`\t\n")
    if not text or _is_technical_noise(text):
        return None

    is_cta_link = any(
        target.lower().startswith("tel:") or _looks_like_cta(label)
        for label, target in link_targets
    )
    kind = "cta" if is_cta_link or (len(text) <= 120 and _looks_like_cta(text)) else "text"
    return text[:360], kind


def _meaningful_image_alt(value: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()
    generic = {
        "", "image", "photo", "picture", "placeholder", "google", "map", "icon",
        "logo", "logo white", "logo dark", "white logo", "dark logo",
    }
    return normalized not in generic and len(normalized) >= 4


def _looks_like_cta(value: str) -> bool:
    return bool(re.search(
        r"\b(?:call|book|schedule|request|contact|get (?:a )?(?:quote|estimate)|learn more|start|apply)\b",
        value,
        flags=re.IGNORECASE,
    ))


def _is_technical_noise(value: str) -> bool:
    lowered = value.casefold()
    if any(marker in lowered for marker in ("data:image", "base64,", "<svg", "viewbox=", "xmlns=", "wp-content/uploads")):
        return True
    if len(value) > 220 and (value.count("%") >= 5 or value.count("/") >= 8):
        return True
    return False


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


def _page_rule_evidence(
    rule_id: int,
    ledger: dict[str, dict[str, Any]],
    context: dict[str, Any],
) -> dict[str, Any]:
    """Select one raw page excerpt using the rule's fixed evidence scope."""
    candidates = [
        item
        for item in ledger.values()
        if str(item.get("source_type") or "") in PAGE_SOURCE_TYPES
        and (rule_id == 6 or str(item.get("evidence_kind") or "text") != "image")
    ]
    candidates.sort(key=lambda item: 1 if str(item.get("evidence_kind") or "text") == "cta" else 0)
    terms = [*RULE_EVIDENCE_TERMS.get(rule_id, ()), *_page_fact_terms(rule_id, context)]
    lowered_terms = [term.casefold() for term in terms if len(term.strip()) >= 3]
    selected = next(
        (
            item
            for item in candidates
            if any(term in str(item.get("extracted_text") or "").casefold() for term in lowered_terms)
        ),
        candidates[0] if candidates else None,
    )
    if selected:
        return _ledger_item_to_evidence(selected, rule_id)

    finding = RULE_FINDING_LABELS.get(rule_id, f"Rule {rule_id} was triggered.")
    return {
        "id": f"ev-rule-{rule_id}-scope",
        "source_type": "page",
        "source_label": "Checked page scope",
        "source_url": str(context.get("url") or "") or None,
        "page_section": "Main page",
        "extracted_text": None,
        "normalized_value": "No usable page excerpt was available in the completed scrape.",
        "expected_value": finding,
        "comparison_result": "partial",
        "confidence": "low",
        "explanation": finding,
    }


def _page_fact_terms(rule_id: int, context: dict[str, Any]) -> list[str]:
    facts = context.get("page_facts") if isinstance(context.get("page_facts"), dict) else {}
    fields_by_rule = {
        1: ("service_areas", "addresses"),
        17: ("business_names",),
        18: ("business_names",),
        19: ("business_names", "phones", "addresses"),
        20: ("business_names", "service_areas"),
    }
    terms: list[str] = []
    for field in fields_by_rule.get(rule_id, ()):
        terms.extend(_values(facts.get(field)))
    return terms


def _review_rule_evidence(
    rule_id: int,
    ledger: dict[str, dict[str, Any]],
    context: dict[str, Any],
) -> list[dict[str, Any]]:
    reviews = [
        item
        for item in ledger.values()
        if str(item.get("source_type") or "") == "review"
    ]
    if reviews:
        return [_ledger_item_to_evidence(item, rule_id) for item in reviews[:3]]

    finding = RULE_FINDING_LABELS.get(rule_id, f"Rule {rule_id} review condition was triggered.")
    gbp_available = bool(context.get("gbp_data"))
    checked_scope = "Page review and testimonial sections"
    if gbp_available:
        checked_scope += " plus the returned recent GBP review sample"
    return [{
        "id": f"ev-rule-{rule_id}-review-scope",
        "source_type": "review",
        "source_label": "Checked review corpus",
        "source_url": str(context.get("url") or "") or None,
        "page_section": checked_scope,
        "extracted_text": None,
        "normalized_value": "No review text was found in the review corpus available to this task.",
        "expected_value": finding,
        "comparison_result": "missing",
        "confidence": "high",
        "explanation": finding,
    }]


def _missing_observation_evidence(
    rule_id: int,
    ledger: dict[str, dict[str, Any]],
    context: dict[str, Any],
) -> dict[str, Any]:
    sub_pages = context.get("sub_pages") if isinstance(context.get("sub_pages"), list) else []
    scope = "Main page"
    if sub_pages:
        scope = f"Main page and {len(sub_pages)} successfully checked internal page(s)"
    finding = RULE_FINDING_LABELS.get(rule_id, f"Rule {rule_id} condition was not found.")
    strategy = MISSING_EVIDENCE_STRATEGIES.get(rule_id, {})
    source_label = str(strategy.get("label") or "Checked page scope")
    contextual_items = _contextual_page_observations(rule_id, ledger)
    if contextual_items:
        return {
            "id": f"ev-rule-{rule_id}-context",
            "source_type": "page",
            "source_label": source_label,
            "source_url": str(context.get("url") or "") or None,
            "page_section": contextual_items[0].get("page_section") or scope,
            "extracted_text": "\n".join(str(item.get("extracted_text") or "") for item in contextual_items),
            "normalized_value": None,
            "expected_value": finding,
            "comparison_result": "partial",
            "confidence": "high",
            "explanation": finding,
        }
    return {
        "id": f"ev-rule-{rule_id}-missing",
        "source_type": "page",
        "source_label": source_label,
        "source_url": str(context.get("url") or "") or None,
        "page_section": scope,
        "extracted_text": None,
        "normalized_value": SHORT_MISSING_LABELS.get(rule_id, finding),
        "expected_value": finding,
        "comparison_result": "missing",
        "confidence": "high",
        "explanation": finding,
    }


def _contextual_page_observations(
    rule_id: int,
    ledger: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    strategy = MISSING_EVIDENCE_STRATEGIES.get(rule_id, {})
    terms = tuple(str(term).casefold() for term in strategy.get("terms", ()))
    accepted_kinds = set(str(kind) for kind in strategy.get("kinds", ("text",)))
    candidates: list[tuple[int, int, dict[str, Any]]] = []
    for index, item in enumerate(ledger.values()):
        # These rules assess the requested page. Supporting pages must not be
        # presented as if their copy appeared on the target page.
        if str(item.get("source_type") or "") != "page":
            continue
        text = str(item.get("extracted_text") or "").strip()
        kind = str(item.get("evidence_kind") or "text")
        if not text or _is_technical_noise(text):
            continue
        if kind not in accepted_kinds:
            continue
        lowered = text.casefold()
        score = sum(2 if " " in term else 1 for term in terms if term in lowered)
        if strategy.get("kinds"):
            score += 4
        if score:
            candidates.append((score, -index, item))

    candidates.sort(key=lambda entry: (entry[0], entry[1]), reverse=True)
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for _, _, item in candidates:
        text = str(item.get("extracted_text") or "").casefold()
        if text in seen:
            continue
        seen.add(text)
        selected.append(item)
        if len(selected) >= 3:
            break
    return selected


def _gbp_comparison_evidence(rule_id: int, context: dict[str, Any]) -> list[dict[str, Any]]:
    page_field, gbp_field = GBP_COMPARISON_FIELDS[rule_id]
    page_facts = context.get("page_facts") if isinstance(context.get("page_facts"), dict) else {}
    gbp = context.get("gbp_data") if isinstance(context.get("gbp_data"), dict) else {}
    page_values = _values(page_facts.get(page_field))
    gbp_values = _values(gbp.get(gbp_field))
    field_label = gbp_field.replace("_", " ")
    finding = RULE_FINDING_LABELS[rule_id]
    backend_findings = context.get("backend_gbp_findings")
    backend_finding = (
        backend_findings.get(f"rule_{rule_id}")
        if isinstance(backend_findings, dict)
        else None
    )
    condition = str(backend_finding.get("condition") or "mismatch") if isinstance(backend_finding, dict) else "mismatch"
    explanation = (
        str(backend_finding.get("explanation") or finding)
        if isinstance(backend_finding, dict)
        else finding
    )
    if isinstance(backend_finding, dict):
        page_values = _values(backend_finding.get("page_values"))
        gbp_values = _values(backend_finding.get("gbp_values"))
    comparison_result = {
        "match": "match",
        "mismatch": "mismatch",
        "page_missing": "missing",
        "gbp_field_missing": "mismatch",
        "both_missing": "not_checked",
        "gbp_unavailable": "not_checked",
    }.get(condition, "mismatch")
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
                "comparison_result": comparison_result,
                "confidence": "high",
                "explanation": explanation,
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
            "explanation": explanation,
        })

    if gbp_values:
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
                "comparison_result": comparison_result,
                "confidence": "high",
                "explanation": explanation,
            })
    else:
        items.append({
            "id": f"ev-rule-{rule_id}-gbp-missing",
            "source_type": "gbp",
            "source_label": f"GBP {field_label}",
            "source_url": str(context.get("gbp_url") or "") or None,
            "page_section": "Google Business Profile",
            "extracted_text": None,
            "normalized_value": "Not returned by the checked GBP response",
            "expected_value": "; ".join(page_values) or "Page field not found",
            "comparison_result": comparison_result,
            "confidence": "high",
            "explanation": explanation,
        })
    return items


def _values(value: Any) -> list[str]:
    if isinstance(value, list):
        return [text for item in value if (text := _source_text(item))]
    if value is None:
        return []
    text = _source_text(value)
    return [text] if text else []


def _source_text(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value).strip()


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
