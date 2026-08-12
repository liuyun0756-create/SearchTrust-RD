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
    7: {"label": "First-person work detail", "terms": ("our technician", "our team", "we diagnosed", "we inspected", "we repaired", "we installed")},
    8: {"label": "Calls to action", "kinds": ("cta",), "terms": ("call", "book", "schedule", "quote", "estimate", "contact", "request")},
    9: {"label": "Operational responsibility", "terms": ("our technician", "our team will", "we will", "handled by", "responsible for", "service request", "follow up")},
    10: {"label": "Limits and complex cases", "terms": ("limitation", "exclusion", "complex case", "additional work", "parts availability", "not included", "cannot", "escalat")},
    11: {"label": "Verifiable trust signals", "terms": ("license", "licensed", "insured", "certified", "award", "association", "warranty")},
    12: {"label": "Outcome and follow-up", "terms": ("warranty", "guarantee", "follow up", "return visit", "correction", "make it right", "satisfaction")},
    14: {"label": "Page purpose and value", "terms": ("service", "repair", "install", "why choose", "benefit")},
    21: {"label": "Business identity", "terms": ("company", "business", "plumbing", "contractor", "contact")},
    22: {"label": "Address", "terms": ("address", "location", "contact", "street", "road", "avenue")},
    23: {"label": "Phone", "kinds": ("cta", "text"), "terms": ("call", "phone", "tel", "contact")},
    24: {"label": "Service area", "terms": ("serving", "service area", "areas we serve", "community", "city")},
    25: {"label": "Business hours", "kinds": ("text", "cta"), "terms": ("hours", "open", "24/7", "monday", "saturday", "sunday")},
    30: {"label": "Geographic context", "terms": ("serving", "service area", "community", "neighborhood", "city", "county")},
    31: {"label": "Landmark reference", "terms": ("landmark", "near ", "next to", "located by", "downtown", "district")},
    32: {"label": "Local context", "terms": ("local", "serving", "service area", "community", "neighborhood", "city")},
    33: {"label": "Service boundary", "terms": ("miles", "radius", "boundary", "surrounding", "nearby", "coverage")},
    34: {"label": "Specific service case", "terms": ("job", "project", "customer", "homeowner", "technician found", "technician repaired")},
    35: {"label": "Customer context", "terms": ("customer", "homeowner", "business owner", "property", "home", "commercial")},
    36: {"label": "Time and activity context", "terms": ("same day", "today", "recent", "last", "since", "year", "hour", "minute")},
}

SHORT_MISSING_LABELS: dict[int, str] = {
    3: "Not found: a real-world geographic or entity anchor, such as a neighborhood, landmark, or local institution.",
    4: "Not found: meaningful time or activity evidence, such as a dated job, recent project, or service timeline.",
    6: "Not found: clearly original imagery, such as real team, vehicle, jobsite, or completed-work photos.",
    7: "Not found: concrete first-person work detail, such as what the team inspected, diagnosed, repaired, or installed.",
    8: "Not found: a page-specific next step. Examples include booking this service, requesting a service-specific estimate, or calling about this exact need.",
    9: "Not found: a clear statement of who handles the service request and owns delivery.",
    10: "Not found: practical limits or complex-case guidance, such as exclusions, escalation, parts constraints, or additional work.",
    11: "Not found: a verifiable trust signal, such as a license, certification, association, award, or warranty.",
    12: "Not found: outcome or follow-up accountability, such as a guarantee, correction process, return visit, or named follow-up.",
    14: "Not found: a distinct reason for this page to exist separately, such as unique service proof, expertise, or local value.",
    21: "Not found: a clear business identity, such as a visible canonical business name tied to the service.",
    22: "Not found: a visible street address, such as the business's customer-facing service location.",
    23: "Not found: a visible contact phone number that a customer can use for this business.",
    24: "Not found: a clear service-area statement, such as named cities, communities, or a defined coverage area.",
    25: "Not found: visible business hours, such as weekday hours, weekend hours, or 24-hour availability.",
    30: "Not found: community-level location detail, such as a neighborhood, district, or named nearby community.",
    31: "Not found: a concrete landmark reference, such as a known road, facility, district, or place near the service area.",
    32: "Not found: factual local operating context, such as local conditions, property types, regulations, or service realities.",
    33: "Not found: a service radius or operating boundary, such as mileage, surrounding communities, or a defined coverage limit.",
    34: "Not found: a specific service case, such as a real customer problem, diagnosis, completed job, or outcome.",
    35: "Not found: a concrete customer situation, such as the property type, problem context, or service need.",
    36: "Not found: meaningful time context, such as when work occurred, how long it took, or when service is available.",
}

REVIEW_EVIDENCE_LABELS: dict[int, str] = {
    37: "Review service detail",
    38: "Review geographic context",
    39: "Review topic alignment",
}

REVIEW_MISSING_LABELS: dict[int, str] = {
    37: "Not found: review text that names the specific service performed, problem handled, or outcome delivered.",
    38: "Not found: review text with geographic context, such as a city, neighborhood, or local service area.",
    39: "Not found: review text whose service topic can be compared with the focus of this page.",
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
        elif rule_id in {21, 22, 23, 24, 25} and isinstance(
            context.get("backend_entity_presence"), dict
        ):
            evidence.extend(_entity_presence_evidence(rule_id, context))
        elif rule_id in {37, 38, 39}:
            evidence.extend(_review_rule_evidence(rule_id, ledger, context))
        elif rule_id in MISSING_OBSERVATION_RULES:
            evidence.append(_missing_observation_evidence(rule_id, ledger, context))
        else:
            evidence.append(_page_rule_evidence(rule_id, ledger, context))

    return _dedupe_evidence(evidence)


def _entity_presence_evidence(
    rule_id: int,
    context: dict[str, Any],
) -> list[dict[str, Any]]:
    """Bind L2 evidence to the same Page Facts that own Rules 21-25."""
    payload = context.get("backend_entity_presence")
    findings = payload.get("findings") if isinstance(payload, dict) else None
    finding = findings.get(f"rule_{rule_id}") if isinstance(findings, dict) else None
    if not isinstance(finding, dict):
        return []

    page_values = _values(finding.get("page_values"))
    page_observations = (
        finding.get("page_observations")
        if isinstance(finding.get("page_observations"), list)
        else []
    )
    explanation = str(
        finding.get("explanation")
        or RULE_FINDING_LABELS.get(rule_id, f"Rule {rule_id} triggered.")
    )
    field_label = str(finding.get("field") or "page field").replace("_", " ").title()

    if not page_values:
        return [{
            "id": f"ev-rule-{rule_id}-missing",
            "source_type": "page",
            "source_label": field_label,
            "source_url": str(context.get("url") or "") or None,
            "page_section": "Checked target page facts",
            "extracted_text": None,
            "normalized_value": SHORT_MISSING_LABELS.get(rule_id, explanation),
            "expected_value": RULE_FINDING_LABELS.get(rule_id),
            "comparison_result": "missing",
            "confidence": "high",
            "explanation": explanation,
        }]

    evidence: list[dict[str, Any]] = []
    for index, value in enumerate(page_values[:4], start=1):
        observation = next(
            (
                item for item in page_observations
                if isinstance(item, dict)
                and str(item.get("raw_value") or item.get("value") or "").strip() == value
            ),
            {},
        )
        evidence.append({
            "id": f"ev-rule-{rule_id}-page-{index:02d}",
            "source_type": "page",
            "source_label": str(observation.get("source_label") or field_label),
            "source_url": str(observation.get("source_url") or context.get("url") or "") or None,
            "page_section": str(observation.get("locator") or "Checked target page facts"),
            "extracted_text": value,
            "normalized_value": observation.get("normalized_value"),
            "expected_value": RULE_FINDING_LABELS.get(rule_id),
            "comparison_result": "partial",
            "confidence": "high",
            "explanation": explanation,
        })
    return evidence


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
        "source_label": REVIEW_EVIDENCE_LABELS.get(rule_id, "Checked review corpus"),
        "source_url": str(context.get("url") or "") or None,
        "page_section": checked_scope,
        "extracted_text": None,
        "normalized_value": REVIEW_MISSING_LABELS.get(
            rule_id,
            "Not found: review text in the review corpus available to this task.",
        ),
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
    page_observations = (
        backend_finding.get("page_observations")
        if isinstance(backend_finding, dict) and isinstance(backend_finding.get("page_observations"), list)
        else []
    )
    comparison_result = {
        "match": "match",
        "mismatch": "missing",
        "page_missing": "missing",
        "gbp_field_missing": "missing",
        "both_missing": "not_checked",
        "gbp_unavailable": "not_checked",
        "field_not_applicable": "not_applicable",
    }.get(condition, "mismatch")
    items: list[dict[str, Any]] = []

    if page_values:
        for index, value in enumerate(page_values[:4], start=1):
            observation = next(
                (
                    item for item in page_observations
                    if isinstance(item, dict) and str(item.get("value") or "") == value
                ),
                {},
            )
            items.append({
                "id": f"ev-rule-{rule_id}-page-{index:02d}",
                "source_type": "page",
                "source_label": str(observation.get("source_label") or f"Page {field_label}"),
                "source_url": str(observation.get("source_url") or context.get("url") or "") or None,
                "page_section": str(observation.get("locator") or "Structured page-to-GBP comparison"),
                "extracted_text": value,
                "normalized_value": observation.get("normalized_value"),
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
    seen: set[tuple[str, str, str, str]] = set()
    for item in items:
        evidence_id = str(item.get("id") or "")
        rule_match = re.search(r"(?:^|-)rule-(\d+)(?:-|$)", evidence_id)
        rule_key = rule_match.group(1) if rule_match else evidence_id
        key = (
            rule_key,
            str(item.get("source_type")),
            str(item.get("extracted_text") or item.get("normalized_value")),
            str(item.get("comparison_result")),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result
