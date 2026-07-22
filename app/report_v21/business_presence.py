"""Backend-owned objective checks for the Business Presence Audit."""

from __future__ import annotations

import copy
import re
from collections import Counter
from typing import Any
from urllib.parse import urlparse

from app.report_v21.coverage import build_gbp_status


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
    comparisons = _build_comparisons(page, gbp, gbp_status)
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
    """Reuse objective alignment records in L3 and related report findings."""
    bound_report = copy.deepcopy(report)
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
        return bound_report

    for layer in bound_report.get("layers", []):
        if isinstance(layer, dict) and layer.get("layer_key") == "entity_consistency":
            layer["evidence_items"] = _merge_evidence(layer.get("evidence_items"), evidence)

    for issue in bound_report.get("key_issues", []):
        if isinstance(issue, dict) and issue.get("affected_layer") == "entity_consistency":
            related_rule_ids = issue.get("related_rule_ids") if isinstance(issue.get("related_rule_ids"), list) else []
            issue_evidence = [
                evidence_by_key[key]
                for rule_id in related_rule_ids
                if (key := _GBP_RULE_TO_COMPARISON_KEY.get(rule_id)) in evidence_by_key
            ]
            issue["evidence_items"] = _merge_evidence(issue.get("evidence_items"), issue_evidence)

    blocker = bound_report.get("primary_blocking_layer")
    if isinstance(blocker, dict) and blocker.get("layer_key") == "entity_consistency":
        blocker["evidence_items"] = _merge_evidence(blocker.get("evidence_items"), evidence)
    return bound_report


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


def _build_comparisons(page: dict[str, Any], gbp: dict[str, Any], gbp_status: str) -> list[dict[str, Any]]:
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


def _comparison_to_evidence(item: dict[str, Any], source_url: str | None) -> dict[str, Any] | None:
    status = item.get("status")
    if status not in {"match", "missing", "mismatch", "partial"}:
        return None
    return {
        "id": item.get("evidence_id") or f"bp-{item.get('key', 'signal')}",
        "source_type": "page",
        "source_label": f"Business Presence Audit: {item.get('label', 'Observed signal')}",
        "source_url": source_url,
        "page_section": "Backend page and GBP comparison",
        "extracted_text": item.get("page_value"),
        "normalized_value": item.get("page_value"),
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
        if not observed:
            return "not_checked", "The public GBP response did not expose service-area data."
        if not gbp_value and applicable is False:
            return "not_applicable", "The GBP source identifies a storefront where service-area data is not applicable."
        if not gbp_value and applicable is True:
            if page_value:
                return "missing", "The page names a service area, while the authoritative GBP source explicitly returned none."
            return "missing", "The authoritative GBP source explicitly returned no service area."

    if not page_value and not gbp_value:
        return "not_checked", "Neither source exposed a usable value for this signal."
    if not page_value:
        return "missing", "GBP exposes this signal, but the scraped page did not expose a comparable value."
    if not gbp_value:
        return "not_checked", "The page exposes this signal, but the public GBP response did not return it."

    left = _normalize(page_value, mode)
    right = _normalize(gbp_value, mode)
    if left == right:
        return "match", "The normalized page and GBP values match."

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
            "title": "Add current business photos to the GBP profile",
            "rationale": "The public GBP response explicitly returned zero photos.",
            "recommended_scope": [
                "Publish current exterior, team, service and completed-work photos.",
                "Use only authentic business-owned images with client permission where required.",
            ],
            "evidence_keys": ["profile-photo-count"],
        })
        profile_actions += 1
    if profile.get("post_status") == "checked" and profile.get("post_count") == 0:
        actions.append({
            "id": "bp-action-add-posts",
            "priority": "low",
            "business_area": "profile_activity",
            "title": "Publish an initial GBP update",
            "rationale": "The public GBP response explicitly returned zero posts.",
            "recommended_scope": [
                "Publish one accurate service, availability or proof-led update.",
                "Do not claim an inactivity period because post dates were not available.",
            ],
            "evidence_keys": ["profile-post-count"],
        })
        profile_actions += 1

    review_actions = 0
    low_unanswered = int(reviews.get("low_rating_unanswered_count") or 0)
    unanswered = int(reviews.get("unanswered_count") or 0)
    detailed_positive = int(reviews.get("detailed_positive_count") or 0)
    if reviews.get("status") in {"checked", "partial"} and low_unanswered:
        actions.append({
            "id": "bp-action-low-rating-replies",
            "priority": "high",
            "business_area": "review_operations",
            "title": "Respond to low-rating reviews first",
            "rationale": f"{low_unanswered} of the recent 1-3 star review(s) have no owner reply.",
            "recommended_scope": [
                "Draft factual, non-defensive replies for each unanswered 1-3 star review.",
                "Escalate service-recovery cases before publishing a response.",
            ],
            "evidence_keys": ["reviews-low-rating-unanswered"],
        })
        review_actions += 1
    remaining_unanswered = max(0, unanswered - low_unanswered)
    if reviews.get("status") in {"checked", "partial"} and remaining_unanswered:
        actions.append({
            "id": "bp-action-review-backlog",
            "priority": "medium",
            "business_area": "review_operations",
            "title": "Clear the remaining recent-review reply backlog",
            "rationale": f"{remaining_unanswered} additional recent review(s) have no owner reply.",
            "recommended_scope": [
                "Prepare concise, specific replies for the remaining unanswered recent reviews.",
                "Keep replies individualized and avoid repetitive templates.",
            ],
            "evidence_keys": ["reviews-unanswered"],
        })
        review_actions += 1
    if reviews.get("status") in {"checked", "partial"} and detailed_positive:
        actions.append({
            "id": "bp-action-proof-candidates",
            "priority": "low",
            "business_area": "review_operations",
            "title": "Review detailed positive feedback for proof opportunities",
            "rationale": f"{detailed_positive} recent 4-5 star review(s) contain at least 80 characters of customer detail.",
            "recommended_scope": [
                "Shortlist useful proof themes without changing the reviewer meaning.",
                "Obtain client approval and follow platform rules before reusing any quote.",
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
        headline = f"{len(actions)} proposal-ready work item(s) identified"
        summary = (
            "The checks below translate objective page, GBP and recent-review observations into "
            "a one-time audit scope. They do not change the eight-layer score."
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
    statuses = {row["status"] for row in rows}
    return "checked" if statuses <= {"match", "missing", "mismatch", "partial"} else "partial"


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
