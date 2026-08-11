"""Backend-owned objective checks for the Business Presence Audit."""

from __future__ import annotations

import copy
import re
from collections import Counter
from typing import Any
from urllib.parse import urlparse

from app.report_v21.coverage import build_gbp_status
from app.report_v21.evidence_ledger import build_layer_evidence


_GBP_RULE_TO_COMPARISON_KEY = {
    26: "business_name",
    27: "address",
    28: "phone",
    29: "service_area",
}


_US_ADDRESS = re.compile(
    r"\b\d{1,6}\s+[A-Za-z0-9.'#\- ]{2,55}\s+"
    r"(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Drive|Dr|Lane|Ln|Court|Ct|"
    r"Parkway|Pkwy|Highway|Hwy|Way|Circle|Cir|Trail|Trl)\.?"
    r"(?:\s*(?:Suite|Ste|Unit|#)\s*[A-Za-z0-9\-]+)?"
    r"(?:,?\s+[A-Za-z .'-]{2,35},?\s+[A-Z]{2}\s+\d{5}(?:-\d{4})?)?\b",
    re.IGNORECASE,
)
_PHONE = re.compile(r"(?:\+?1[\s.\-]?)?\(?\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4}")
_HOURS_LINE = re.compile(
    r"^.{0,45}(?:\bMon(?:day)?\b|\bTue(?:sday)?\b|\bWed(?:nesday)?\b|"
    r"\bThu(?:rsday)?\b|\bFri(?:day)?\b|\bSat(?:urday)?\b|\bSun(?:day)?\b|"
    r"\bopen\s+24\s+hours\b|\b24/7\b).{0,100}$",
    re.IGNORECASE | re.MULTILINE,
)
_SERVICE_AREA_LINE = re.compile(
    r"^.{0,25}(?:serving|service areas?|areas? we serve|proudly serving)\b.{0,180}$",
    re.IGNORECASE | re.MULTILINE,
)
_HEADING = re.compile(r"^#{1,2}\s+(.{3,140})$", re.MULTILINE)
_MARKDOWN_LINK_ONLY = re.compile(r"^\[[^\]]+\]\([^)]+\)$")


def build_business_presence_audit(context: dict[str, Any]) -> dict[str, Any]:
    """Build a non-scoring audit from raw backend page and GBP observations."""
    content = _text(context.get("page_content"))
    page_business = context.get("page_business")
    if not isinstance(page_business, dict):
        page_business = {}
    gbp = context.get("gbp_data")
    if not isinstance(gbp, dict):
        gbp = {}

    gbp_status = build_gbp_status(context)["status"]
    page = _extract_page_signals(content, page_business, _text(context.get("url")))
    backend_findings = context.get("backend_gbp_findings")
    if not isinstance(backend_findings, dict):
        # Historical reports and direct callers must still use the canonical
        # L3 facts/comparator instead of maintaining a second interpretation.
        from app.report_v21.gbp_rule_evaluator import evaluate_gbp_rules  # noqa: PLC0415
        from app.report_v21.page_facts import build_page_facts  # noqa: PLC0415

        page_facts = context.get("page_facts")
        if not isinstance(page_facts, dict):
            page_facts = build_page_facts(
                content,
                page_business,
                context.get("target_identity_signals"),
            )
        derived_context = dict(context)
        derived_context["page_facts"] = page_facts
        _, _, backend_findings = evaluate_gbp_rules(derived_context)
    comparisons = _build_comparisons(
        page,
        gbp,
        gbp_status,
        backend_findings if isinstance(backend_findings, dict) else {},
    )
    profile = _build_profile_activity(gbp, gbp_status)
    reviews = _build_review_audit(gbp, gbp_status)

    assessed = [item for item in comparisons if item["status"] not in {"not_checked", "not_applicable", "error"}]
    matched = [item for item in assessed if item["status"] == "match"]
    issues = [item for item in assessed if item["status"] in {"mismatch", "missing", "partial"}]
    unavailable = [item for item in comparisons if item["status"] in {"not_checked", "not_applicable", "error"}]
    proposal_status, proposal_summary, proposal_actions = _build_proposal(
        comparisons=comparisons,
        profile=profile,
        reviews=reviews,
        gbp_status=gbp_status,
    )

    scope = [
        {
            "key": "page_content",
            "label": "Page content",
            "status": "checked" if content else "not_checked",
            "detail": "Visible page signals were extracted by the backend." if content else "No usable page content was available.",
        },
        {
            "key": "gbp_profile",
            "label": "GBP profile",
            "status": "checked" if gbp_status == "checked" else ("error" if gbp_status == "error" else "not_checked"),
            "detail": _gbp_scope_detail(gbp_status),
        },
        {
            "key": "gbp_page_alignment",
            "label": "GBP x Page alignment",
            "status": _alignment_scope_status(comparisons, gbp_status),
            "detail": f"{len(assessed)} of {len(comparisons)} signals could be assessed objectively.",
        },
        {
            "key": "profile_activity",
            "label": "GBP categories and activity",
            "status": profile["status"],
            "detail": _profile_scope_detail(profile),
        },
        {
            "key": "recent_reviews",
            "label": "Recent review sample",
            "status": reviews["status"],
            "detail": f"{reviews['sample_size']} most recent reviews returned; sample limit is 30.",
        },
        {
            "key": "citations",
            "label": "External citations / NAP network",
            "status": "not_checked",
            "detail": "Citation provider integration is deferred for this release.",
        },
    ]

    return {
        "audit_scope": scope,
        "summary": {
            "assessed_items": len(assessed),
            "matched_items": len(matched),
            "issue_items": len(issues),
            "not_checked_items": len(unavailable),
        },
        "gbp_page_alignment": comparisons,
        "profile_activity": profile,
        "review_audit": reviews,
        "citations": {
            "status": "not_checked",
            "reason": "Citation provider integration is deferred for this release.",
        },
        "proposal_status": proposal_status,
        "proposal_summary": proposal_summary,
        "proposal_actions": proposal_actions,
    }


def bind_business_presence_evidence(
    report: dict[str, Any],
    audit: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, Any]:
    """Bind the canonical rule 26-29 evidence to every L3 presentation.

    Business Presence must not create a second interpretation of the same four
    fields.  The layer, its key issues and its blocker all reuse evidence built
    directly from the backend rule findings.
    """
    bound_report = copy.deepcopy(report)
    if not isinstance(context.get("backend_gbp_findings"), dict):
        return _bind_legacy_business_presence_evidence(bound_report, audit, context)

    layer_rule_ids: list[int] = []

    for layer in bound_report.get("layers", []):
        if isinstance(layer, dict) and layer.get("layer_key") == "entity_consistency":
            layer_rule_ids = _l3_rule_ids(layer.get("triggered_rule_ids"))
            layer["evidence_items"] = build_layer_evidence(layer_rule_ids, {}, context)

    for issue in bound_report.get("key_issues", []):
        if isinstance(issue, dict) and issue.get("affected_layer") == "entity_consistency":
            related_rule_ids = _l3_rule_ids(issue.get("related_rule_ids"))
            issue["evidence_items"] = build_layer_evidence(related_rule_ids, {}, context)

    blocker = bound_report.get("primary_blocking_layer")
    if isinstance(blocker, dict) and blocker.get("layer_key") == "entity_consistency":
        blocker["evidence_items"] = build_layer_evidence(layer_rule_ids, {}, context)
    return bound_report


def _bind_legacy_business_presence_evidence(
    report: dict[str, Any],
    audit: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, Any]:
    """Preserve evidence behavior for stored reports without backend findings."""
    source_url = _optional_text(context.get("url"))
    evidence_by_key = {
        str(item.get("key")): converted
        for item in audit.get("gbp_page_alignment", [])
        if isinstance(item, dict)
        and item.get("status") in {"match", "missing", "mismatch", "partial"}
        and (converted := _comparison_to_evidence(item, source_url))
    }
    evidence = list(evidence_by_key.values())
    if not evidence:
        return report

    for layer in report.get("layers", []):
        if isinstance(layer, dict) and layer.get("layer_key") == "entity_consistency":
            layer["evidence_items"] = _merge_evidence(layer.get("evidence_items"), evidence)

    for issue in report.get("key_issues", []):
        if isinstance(issue, dict) and issue.get("affected_layer") == "entity_consistency":
            related_rule_ids = issue.get("related_rule_ids") if isinstance(issue.get("related_rule_ids"), list) else []
            issue_evidence = [
                evidence_by_key[key]
                for rule_id in related_rule_ids
                if (key := _GBP_RULE_TO_COMPARISON_KEY.get(rule_id)) in evidence_by_key
            ]
            issue["evidence_items"] = _merge_evidence(issue.get("evidence_items"), issue_evidence)

    blocker = report.get("primary_blocking_layer")
    if isinstance(blocker, dict) and blocker.get("layer_key") == "entity_consistency":
        blocker["evidence_items"] = _merge_evidence(blocker.get("evidence_items"), evidence)
    return report


def _l3_rule_ids(value: Any) -> list[int]:
    return [
        rule_id
        for rule_id in value
        if isinstance(rule_id, int) and rule_id in _GBP_RULE_TO_COMPARISON_KEY
    ] if isinstance(value, list) else []


def _extract_page_signals(content: str, business: dict[str, Any], url: str) -> dict[str, Any]:
    headings = [_clean_excerpt(value) for value in _HEADING.findall(content)]
    address = _first_match(_US_ADDRESS, content)
    phone = _clean_phone(_text(business.get("phone"))) or _clean_phone(content)
    hours = _first_match(_HOURS_LINE, content)
    service_area = _first_match(_SERVICE_AREA_LINE, content)
    category = headings[0] if headings else None

    return {
        "business_name": _text(business.get("name")) or None,
        "phone": phone or None,
        "address": address or None,
        "website": url or None,
        "opening_hours": hours or None,
        "service_area": service_area or _text(business.get("city")) or None,
        "categories": [category] if category else [],
    }


def _build_comparisons(
    page: dict[str, Any],
    gbp: dict[str, Any],
    gbp_status: str,
    backend_findings: dict[str, Any],
) -> list[dict[str, Any]]:
    specs = (
        ("business_name", "Business name", "name", "text"),
        ("phone", "Phone", "phone", "phone"),
        ("address", "Address", "address", "text"),
        ("website", "Website", "website", "url"),
        ("opening_hours", "Opening hours", "hours", "hours"),
        ("service_area", "Service area", "service_areas", "service_area"),
        ("categories", "Categories / service intent", "categories", "tokens"),
    )
    rows: list[dict[str, Any]] = []
    for key, label, gbp_key, mode in specs:
        rule_id = next(
            (candidate for candidate, comparison_key in _GBP_RULE_TO_COMPARISON_KEY.items() if comparison_key == key),
            None,
        )
        finding = backend_findings.get(f"rule_{rule_id}") if rule_id is not None else None
        if isinstance(finding, dict):
            page_value = finding.get("page_values")
            gbp_value = finding.get("gbp_values")
            status = _finding_status(str(finding.get("condition") or "gbp_unavailable"))
            explanation = str(finding.get("explanation") or "Backend GBP comparison.")
        else:
            page_value = page.get(key)
            gbp_value = gbp.get(gbp_key)
            status, explanation = _compare_signal(
                key=key,
                page_value=page_value,
                gbp_value=gbp_value,
                gbp=gbp,
                gbp_status=gbp_status,
                mode=mode,
            )
        rows.append({
            "key": key,
            "evidence_id": f"bp-{key}",
            "label": label,
            "status": status,
            "page_value": _display(page_value),
            "gbp_value": _display(gbp_value),
            "page_source": "Observed in scraped page content" if page_value else None,
            "gbp_source": "Observed in public GBP result" if gbp_value not in (None, "", []) else None,
            "explanation": explanation,
            "related_layer": "entity_consistency",
            "included_in_score": False,
        })
    return rows


def _finding_status(condition: str) -> str:
    return {
        "match": "match",
        "mismatch": "missing",
        "page_missing": "missing",
        "gbp_field_missing": "missing",
        "both_missing": "not_checked",
        "gbp_unavailable": "not_checked",
        "field_not_applicable": "not_applicable",
    }.get(condition, "not_checked")


def _comparison_to_evidence(item: dict[str, Any], source_url: str | None) -> dict[str, Any] | None:
    status = item.get("status")
    if status not in {"match", "missing", "mismatch", "partial"}:
        return None
    page_value = item.get("page_value")
    normalized_value = page_value
    if status == "missing" and not _optional_text(page_value):
        normalized_value = "Not found in the checked page scope"
    return {
        "id": item.get("evidence_id") or f"bp-{item.get('key', 'signal')}",
        "source_type": "page",
        "source_label": f"Business Presence Audit: {item.get('label', 'Observed signal')}",
        "source_url": source_url,
        "page_section": "Backend page and GBP comparison",
        "extracted_text": page_value,
        "normalized_value": normalized_value,
        "expected_value": item.get("gbp_value"),
        "comparison_result": status,
        "confidence": "high" if status in {"match", "mismatch"} else "medium",
        "explanation": item.get("explanation") or "Objective backend comparison.",
    }


def _merge_evidence(current: Any, additions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = [item for item in current if isinstance(item, dict)] if isinstance(current, list) else []
    seen = {str(item.get("id")) for item in result}
    for item in additions:
        if str(item.get("id")) not in seen:
            result.append(copy.deepcopy(item))
            seen.add(str(item.get("id")))
    return result


def _compare_signal(
    *,
    key: str,
    page_value: Any,
    gbp_value: Any,
    gbp: dict[str, Any],
    gbp_status: str,
    mode: str,
) -> tuple[str, str]:
    if gbp_status != "checked":
        return "not_checked", "GBP data was not available, so no comparison was made."

    if key == "service_area":
        observed = bool(gbp.get("service_areas_observed"))
        applicable = gbp.get("service_area_business")
        if not gbp_value and applicable is False:
            return "not_applicable", "The GBP source identifies a storefront where service-area data is not applicable."
        if not observed:
            if page_value and not gbp_value:
                return "missing", "The page identifies a service area, but the checked GBP response did not return one."
            return "not_checked", "The public GBP response did not expose verifiable service-area data."
        if not gbp_value and applicable is True:
            if page_value:
                return "missing", "The page names a service area, while the authoritative GBP source explicitly returned none."
            return "missing", "The authoritative GBP source explicitly returned no service area."

    if not page_value and not gbp_value:
        return "not_checked", "Neither source exposed a usable value for this signal."
    if not page_value:
        return "missing", "GBP exposes this signal, but the scraped page did not expose a comparable value."
    if not gbp_value:
        if key in _GBP_RULE_TO_COMPARISON_KEY.values():
            return "missing", "The page exposes this signal, but the public GBP response did not return it."
        return "not_checked", "The page exposes this signal, but the public GBP response did not return it."

    left = _normalize(page_value, mode)
    right = _normalize(gbp_value, mode)
    if left == right:
        return "match", "The normalized page and GBP values match."

    if key in _GBP_RULE_TO_COMPARISON_KEY.values():
        return "missing", "The normalized page and GBP values are not exactly equal."

    left_tokens = _tokens(left)
    right_tokens = _tokens(right)
    if mode in {"service_area", "tokens"} and left_tokens and left_tokens == right_tokens:
        return "match", "The normalized page and GBP values match."
    if left_tokens and right_tokens and left_tokens.intersection(right_tokens):
        return "partial", "The sources share part of the same signal, but the observed values are not fully aligned."
    return "mismatch", "The normalized page and GBP values do not align."


def _build_profile_activity(gbp: dict[str, Any], gbp_status: str) -> dict[str, Any]:
    categories = _string_list(gbp.get("categories"))
    photo_fetch = gbp.get("photo_fetch") if isinstance(gbp.get("photo_fetch"), dict) else {}
    post_fetch = gbp.get("post_fetch") if isinstance(gbp.get("post_fetch"), dict) else {}
    limitations: list[str] = []

    if gbp_status != "checked":
        return {
            "status": "not_checked",
            "categories": [],
            "category_source": "not_available",
            "photo_count": None,
            "latest_photo_date": None,
            "photo_status": "not_checked",
            "post_count": None,
            "latest_post_date": None,
            "post_status": "not_checked",
            "limitations": ["GBP profile data was unavailable."],
        }

    photo_status = _fetch_status(photo_fetch)
    post_status = _fetch_status(post_fetch)
    if photo_status == "checked" and not photo_fetch.get("latest_date"):
        limitations.append("The public photo response did not expose reliable upload dates.")
    if not categories:
        limitations.append("The public GBP response did not expose categories.")

    observed_parts = [bool(categories), photo_status == "checked", post_status == "checked"]
    status = "checked" if all(observed_parts) else ("partial" if any(observed_parts) else "not_checked")
    return {
        "status": status,
        "categories": categories,
        "category_source": "observed" if categories else "not_available",
        "photo_count": _optional_int(photo_fetch.get("count")),
        "latest_photo_date": _optional_text(photo_fetch.get("latest_date")),
        "photo_status": photo_status,
        "post_count": _optional_int(post_fetch.get("count")),
        "latest_post_date": _optional_text(post_fetch.get("latest_date")),
        "post_status": post_status,
        "limitations": limitations,
    }


def _build_review_audit(gbp: dict[str, Any], gbp_status: str) -> dict[str, Any]:
    total = _optional_int(gbp.get("reviews"))
    raw_reviews = gbp.get("review_list")
    reviews = raw_reviews[:30] if isinstance(raw_reviews, list) else []
    fetch = gbp.get("review_fetch") if isinstance(gbp.get("review_fetch"), dict) else {}
    limitations: list[str] = []

    if gbp_status != "checked":
        status = "not_checked"
        limitations.append("GBP profile data was unavailable.")
    elif reviews:
        status = "checked" if len(reviews) >= min(total or 30, 30) else "partial"
        if status == "partial":
            limitations.append("Fewer than the requested recent-review sample was returned by the public endpoint.")
    elif total and total > 0:
        status = "error" if fetch.get("error") else "partial"
        limitations.append("GBP reports reviews, but the public review endpoint returned no review records.")
    else:
        status = "not_checked"
        limitations.append("No recent review records were returned for analysis.")

    distribution: Counter[str] = Counter()
    owner_reply_count = 0
    unanswered_count = 0
    low_rating_count = 0
    low_rating_unanswered_count = 0
    detailed_positive_count = 0
    clean_reviews: list[dict[str, Any]] = []
    for item in reviews:
        if not isinstance(item, dict):
            continue
        rating = _optional_float(item.get("rating"))
        if rating is not None:
            distribution[str(int(round(rating)))] += 1
        owner_reply = _optional_text(item.get("owner_reply"))
        owner_reply_count += int(bool(owner_reply))
        unanswered_count += int(not owner_reply)
        is_low_rating = rating is not None and rating <= 3
        low_rating_count += int(is_low_rating)
        low_rating_unanswered_count += int(is_low_rating and not owner_reply)
        review_text = _optional_text(item.get("text"))
        detailed_positive_count += int(
            rating is not None and rating >= 4 and bool(review_text) and len(review_text) >= 80
        )
        clean_reviews.append({
            "author": _optional_text(item.get("author")),
            "rating": rating,
            "date": _optional_text(item.get("date")),
            "text": review_text,
            "owner_reply": owner_reply,
        })

    sample_size = len(clean_reviews)
    return {
        "status": status,
        "total_reviews": total,
        "sample_size": sample_size,
        "sample_limit": 30,
        "latest_review_date": clean_reviews[0].get("date") if clean_reviews else None,
        "rating_distribution": dict(sorted(distribution.items())),
        "owner_reply_count": owner_reply_count,
        "owner_reply_rate": (owner_reply_count / sample_size) if sample_size else None,
        "unanswered_count": unanswered_count,
        "low_rating_count": low_rating_count,
        "low_rating_unanswered_count": low_rating_unanswered_count,
        "detailed_positive_count": detailed_positive_count,
        "reviews": clean_reviews,
        "limitations": limitations,
    }


def _build_proposal(
    *,
    comparisons: list[dict[str, Any]],
    profile: dict[str, Any],
    reviews: dict[str, Any],
    gbp_status: str,
) -> tuple[str, dict[str, Any], list[dict[str, Any]]]:
    actions: list[dict[str, Any]] = []
    identity_issues = [
        item for item in comparisons if item.get("status") in {"mismatch", "missing", "partial"}
    ]
    if identity_issues:
        labels = [str(item.get("label") or item.get("key")) for item in identity_issues]
        actions.append({
            "id": "bp-action-identity-alignment",
            "priority": "high" if any(item.get("status") in {"mismatch", "missing"} for item in identity_issues) else "medium",
            "business_area": "identity_alignment",
            "title": "Align the business identity across the page and GBP profile",
            "rationale": f"{len(identity_issues)} objectively compared signal(s) need attention: {', '.join(labels)}.",
            "recommended_scope": [
                f"Confirm the authoritative {label.lower()} and update the conflicting or missing source."
                for label in labels
            ],
            "evidence_keys": [str(item.get("evidence_id")) for item in identity_issues],
        })

    profile_actions = 0
    if profile.get("photo_status") == "checked" and profile.get("photo_count") == 0:
        actions.append({
            "id": "bp-action-add-photos",
            "priority": "medium",
            "business_area": "profile_activity",
            "title": "Create and publish a current GBP photo set",
            "rationale": (
                "The GBP profile has no public photos, so prospective customers cannot see "
                "current visual proof of the business, team or completed work."
            ),
            "recommended_scope": [
                "Prepare an approved set of authentic exterior, team, service and completed-work photos.",
                "Upload the approved images to GBP with clear, accurate descriptions.",
                "Provide the client with a reusable photo checklist for future updates.",
            ],
            "evidence_keys": ["profile-photo-count"],
        })
        profile_actions += 1
    if profile.get("post_status") == "checked" and profile.get("post_count") == 0:
        actions.append({
            "id": "bp-action-add-posts",
            "priority": "low",
            "business_area": "profile_activity",
            "title": "Publish the first GBP business update",
            "rationale": (
                "The GBP profile has no published updates, leaving no recent public signal about "
                "services, availability or completed work."
            ),
            "recommended_scope": [
                "Draft and publish one accurate update about a current service, availability or completed job.",
                "Include an approved image and a clear next step for prospective customers.",
                "Provide a simple topic plan for the next three GBP updates.",
            ],
            "evidence_keys": ["profile-post-count"],
        })
        profile_actions += 1

    review_actions = 0
    low_unanswered = int(reviews.get("low_rating_unanswered_count") or 0)
    unanswered = int(reviews.get("unanswered_count") or 0)
    detailed_positive = int(reviews.get("detailed_positive_count") or 0)
    if reviews.get("status") in {"checked", "partial"} and low_unanswered:
        low_review_label = "review" if low_unanswered == 1 else "reviews"
        actions.append({
            "id": "bp-action-low-rating-replies",
            "priority": "high",
            "business_area": "review_operations",
            "title": "Resolve unanswered low-rating reviews",
            "rationale": (
                f"{low_unanswered} recent 1-3 star {low_review_label} "
                f"{'has' if low_unanswered == 1 else 'have'} no owner reply. "
                f"{'This review needs' if low_unanswered == 1 else 'These reviews need'} a timely "
                "response and may require service-recovery follow-up."
            ),
            "recommended_scope": [
                "Review each unanswered 1-3 star review with the client and identify any service-recovery case.",
                "Prepare a factual, non-defensive, individualized reply for each review.",
                "Deliver an approval-ready reply list and flag cases that need private follow-up before publishing.",
            ],
            "evidence_keys": ["reviews-low-rating-unanswered"],
        })
        review_actions += 1
    remaining_unanswered = max(0, unanswered - low_unanswered)
    if reviews.get("status") in {"checked", "partial"} and remaining_unanswered:
        remaining_review_label = "review" if remaining_unanswered == 1 else "reviews"
        actions.append({
            "id": "bp-action-review-backlog",
            "priority": "medium",
            "business_area": "review_operations",
            "title": "Complete replies for the remaining recent reviews",
            "rationale": (
                f"{remaining_unanswered} additional recent {remaining_review_label} "
                f"{'has' if remaining_unanswered == 1 else 'have'} no owner reply. "
                f"Completing {'this reply' if remaining_unanswered == 1 else 'these replies'} gives "
                "the client a consistent review-response process."
            ),
            "recommended_scope": [
                "Prepare a concise, specific reply for each remaining unanswered recent review.",
                "Personalize every reply to the customer's feedback and avoid repeated templates.",
                "Deliver the replies in an approval-ready list with a recommended publishing order.",
            ],
            "evidence_keys": ["reviews-unanswered"],
        })
        review_actions += 1
    if reviews.get("status") in {"checked", "partial"} and detailed_positive:
        positive_review_label = "review" if detailed_positive == 1 else "reviews"
        actions.append({
            "id": "bp-action-proof-candidates",
            "priority": "low",
            "business_area": "review_operations",
            "title": "Turn detailed reviews into approved customer proof",
            "rationale": (
                f"{detailed_positive} recent 4-5 star {positive_review_label} "
                f"{'includes' if detailed_positive == 1 else 'include'} specific customer detail "
                "that could support relevant service pages, case examples and client proposals."
            ),
            "recommended_scope": [
                "Select 3-5 reviews with specific service, response or outcome details.",
                "Organize the strongest proof into reusable themes without changing the reviewer meaning.",
                "Recommend where each approved quote should appear, such as a service page, case example or proposal.",
                "Obtain client approval and confirm platform rules before publishing any quote.",
            ],
            "evidence_keys": ["reviews-detailed-positive"],
        })
        review_actions += 1

    unavailable = gbp_status != "checked" or any(
        item.get("status") in {"not_checked", "error"} for item in comparisons
    )
    unavailable = unavailable or profile.get("status") in {"not_checked", "partial", "error"}
    unavailable = unavailable or reviews.get("status") in {"not_checked", "partial", "error"}
    status = "needs_attention" if actions else ("limited" if unavailable else "clear")

    if actions:
        headline = f"{len(actions)} client-ready {('task' if len(actions) == 1 else 'tasks')} identified"
        summary = (
            "Each task below explains why the work is needed and what the agency should deliver. "
            "These off-site tasks do not change the eight-layer page score."
        )
    elif status == "limited":
        headline = "No confirmed work item from the available data"
        summary = "Some Business Presence inputs were unavailable, so the audit avoids definite conclusions for those areas."
    else:
        headline = "No confirmed Business Presence issue"
        summary = "The objectively assessed page, GBP and recent-review signals did not produce a proposal task."

    return status, {
        "headline": headline,
        "summary": summary,
        "identity_issue_count": len(identity_issues),
        "profile_opportunity_count": profile_actions,
        "review_action_count": review_actions,
    }, actions


def _fetch_status(value: dict[str, Any]) -> str:
    if value.get("error"):
        return "error"
    if value.get("attempted"):
        return "checked"
    return "not_checked"


def _alignment_scope_status(rows: list[dict[str, Any]], gbp_status: str) -> str:
    if gbp_status == "error":
        return "error"
    if gbp_status != "checked":
        return "not_checked"
    return "checked" if rows else "partial"


def _gbp_scope_detail(status: str) -> str:
    return {
        "checked": "A usable public GBP profile was returned.",
        "error": "GBP lookup failed; comparisons were not generated.",
        "not_found": "GBP lookup completed without a confident profile match.",
    }.get(status, "GBP was not checked for this report.")


def _profile_scope_detail(profile: dict[str, Any]) -> str:
    categories = len(profile.get("categories") or [])
    photos = profile.get("photo_count")
    posts = profile.get("post_count")
    return f"Observed {categories} categories, {photos if photos is not None else 'unknown'} photos and {posts if posts is not None else 'unknown'} posts."


def _normalize(value: Any, mode: str) -> str:
    text = _display(value) or ""
    if mode == "phone":
        digits = re.sub(r"\D", "", text)
        return digits[-10:]
    if mode == "url":
        parsed = urlparse(text if "://" in text else f"https://{text}")
        return parsed.netloc.lower().removeprefix("www.").rstrip("/")
    if mode == "hours" and re.search(r"(?:\b24\s*/\s*7\b|\bopen\s+24\s+hours\b)", text, re.IGNORECASE):
        return "open 24 7"
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _tokens(value: str) -> set[str]:
    ignored = {
        "the", "and", "of", "in", "at", "llc", "inc", "services", "service", "page",
        "serving", "serve", "areas", "area", "nearby", "communities",
    }
    return {token for token in value.split() if len(token) > 2 and token not in ignored}


def _display(value: Any) -> str | None:
    if isinstance(value, list):
        values = [_text(item) for item in value if _text(item)]
        return ", ".join(values) or None
    if isinstance(value, dict):
        values = [f"{key}: {_text(item)}" for key, item in value.items() if _text(item)]
        return "; ".join(values) or None
    return _optional_text(value)


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return list(dict.fromkeys(_text(item) for item in value if _text(item)))
    text = _text(value)
    return [text] if text else []


def _first_match(pattern: re.Pattern[str], content: str) -> str | None:
    for match in pattern.finditer(content):
        candidate = _clean_excerpt(match.group(0))
        if candidate and not _MARKDOWN_LINK_ONLY.fullmatch(candidate):
            return candidate
    return None


def _clean_phone(value: str) -> str | None:
    """Return a comparable phone value without parser punctuation artifacts."""
    if not value:
        return None
    match = _PHONE.search(value)
    if match:
        return _clean_excerpt(match.group(0))
    digits = re.sub(r"\D", "", value)
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    if len(digits) == 10:
        return digits
    return None


def _clean_excerpt(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip(" -*#|\t\n")


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        return max(0, int(str(value).replace(",", "")))
    except (TypeError, ValueError):
        return None


def _optional_float(value: Any) -> float | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_text(value: Any) -> str | None:
    text = _text(value)
    return text or None


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""
