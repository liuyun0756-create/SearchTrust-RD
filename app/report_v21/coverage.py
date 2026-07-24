"""Backend-owned coverage and GBP status helpers for report_v2_1."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse


def build_gbp_status(context: dict[str, Any]) -> dict[str, str | None]:
    """Build backend-owned GBP status from scraper context."""
    input_gbp_url = _optional_str(context.get("input_gbp_url"))
    gbp_url = _optional_str(context.get("gbp_url"))
    gbp_error = _optional_str(context.get("gbp_error"))
    gbp_data = context.get("gbp_data")
    lookup_attempted = bool(context.get("gbp_lookup_attempted"))
    lookup_diagnostic = context.get("gbp_lookup_diagnostic")
    diagnostic_reason = (
        _optional_str(lookup_diagnostic.get("message"))
        if isinstance(lookup_diagnostic, dict)
        else None
    )
    source = _gbp_source(input_gbp_url, gbp_url, gbp_data)

    if gbp_error:
        return {
            "status": "error",
            "source": source,
            "gbp_url": gbp_url,
            "reason": f"GBP lookup failed: {gbp_error}",
        }

    if _has_usable_gbp_data(gbp_data):
        return {
            "status": "checked",
            "source": source,
            "gbp_url": gbp_url,
            "reason": None,
        }

    if not input_gbp_url and lookup_attempted:
        return {
            "status": "not_found",
            "source": source,
            "gbp_url": gbp_url,
            "reason": diagnostic_reason or (
                "GBP auto-discovery was attempted from the checked page, but no confident "
                "usable GBP profile was returned."
            ),
        }

    if not input_gbp_url:
        return {
            "status": "not_checked",
            "source": source,
            "gbp_url": gbp_url,
            "reason": (
                "The system found a possible GBP link or business match, but did not receive usable GBP data, "
                "so GBP alignment was not verified."
                if source == "system_discovered"
                else "No GBP URL was provided by the user, so GBP alignment was not verified."
            ),
        }

    if input_gbp_url and not lookup_attempted:
        return {
            "status": "not_checked",
            "source": source,
            "gbp_url": gbp_url or input_gbp_url,
            "reason": "A GBP URL was provided, but the backend did not confirm that GBP lookup was attempted.",
        }

    if input_gbp_url:
        return {
            "status": "not_found",
            "source": source,
            "gbp_url": gbp_url or input_gbp_url,
            "reason": diagnostic_reason or (
                "GBP lookup appears to have been attempted, but no confident "
                "usable GBP profile was returned. Current scraper output does "
                "not expose a more specific no-match reason."
            ),
        }

    return {
        "status": "not_checked",
        "source": source,
        "gbp_url": gbp_url,
        "reason": "GBP lookup was not attempted because no GBP URL or usable lookup input was available.",
    }


def _gbp_source(input_gbp_url: str | None, gbp_url: str | None, gbp_data: Any) -> str:
    if input_gbp_url:
        return "user_provided"
    if gbp_url or _has_usable_gbp_data(gbp_data):
        return "system_discovered"
    return "not_available"


def build_data_coverage(
    context: dict[str, Any],
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    """Build backend-owned data coverage from scraper context."""
    warnings = list(warnings or [])
    gbp_status = build_gbp_status(context)
    gbp_data = context.get("gbp_data")
    sub_pages = context.get("sub_pages")
    sub_page_paths = _sub_page_paths(sub_pages)

    schema_summary = build_schema_summary(context)
    page_content_checked = bool(context.get("content_checked"))
    gbp_checked = gbp_status["status"] == "checked"
    review_corpus = context.get("review_corpus")
    reviews_checked = page_content_checked or _has_reviews(gbp_data)
    review_text_available = isinstance(review_corpus, list) and bool(review_corpus)
    internal_pages_checked = bool(sub_page_paths)
    contact_page_checked = any("contact" in path for path in sub_page_paths)
    about_page_checked = any("about" in path or "our-story" in path or "who-we-are" in path for path in sub_page_paths)

    limitations = _dedupe([
        *warnings,
        *([] if page_content_checked else ["Page content was not confirmed as checked by the backend."]),
        *([] if schema_summary and schema_summary["checked"] else ["Schema extraction was not available from the current page response."]),
        *(["No JSON-LD schema was detected in the checked page response."] if schema_summary and schema_summary["checked"] and not schema_summary["types"] else []),
        *([] if contact_page_checked else ["No successfully scraped contact page was available for this audit."]),
        *([] if about_page_checked else ["No successfully scraped about page was available for this audit."]),
        *(
            []
            if review_text_available
            else ["No usable review or testimonial text was found in the checked page and GBP sources."]
        ),
        *([] if internal_pages_checked else ["Internal sub-page coverage was not confirmed by the scraper."]),
        "Citations are not checked in this audit.",
        "Competitor pages and map-pack / geo-grid visibility are not checked in this audit.",
        *(
            []
            if gbp_checked
            else ["GBP alignment could not be verified because usable GBP data was not checked."]
        ),
    ])

    return {
        "page_content_checked": page_content_checked,
        "gbp_checked": gbp_checked,
        "schema_checked": bool(schema_summary and schema_summary["checked"]),
        "contact_page_checked": contact_page_checked,
        "about_page_checked": about_page_checked,
        "reviews_checked": reviews_checked,
        "internal_pages_checked": internal_pages_checked,
        "competitor_pages_checked": False,
        "citations_checked": False,
        "geo_grid_checked": False,
        "limitations": limitations,
    }


def build_gbp_profile(context: dict[str, Any]) -> dict[str, Any] | None:
    """Project only verified, presentation-safe GBP fields from scraper data."""
    if build_gbp_status(context)["status"] != "checked":
        return None
    gbp = context.get("gbp_data")
    if not isinstance(gbp, dict):
        return None
    categories = _string_list(gbp.get("type"))
    service_areas = _string_list(gbp.get("service_areas"))
    return {
        "name": _optional_str(gbp.get("name")),
        "address": _optional_str(gbp.get("address")),
        "phone": _optional_str(gbp.get("phone")),
        "website": _optional_str(gbp.get("website")),
        "categories": categories,
        "hours": _optional_str(gbp.get("hours")),
        "rating": _optional_str(gbp.get("rating")),
        "review_count": _optional_str(gbp.get("reviews")),
        "service_areas": service_areas,
    }


def build_schema_summary(context: dict[str, Any]) -> dict[str, Any] | None:
    """Expose only parsed JSON-LD metadata from a directly checked page response."""
    schema = context.get("schema_data")
    if not isinstance(schema, dict) or not schema.get("checked"):
        return None
    return {
        "checked": True,
        "source_url": _optional_str(schema.get("source_url")) or _optional_str(context.get("url")),
        "types": _string_list(schema.get("types")),
    }


def build_gbp_alignment(context: dict[str, Any]) -> list[dict[str, Any]]:
    """Build conservative, deterministic page-to-GBP alignment rows.

    Address and service area are deliberately marked not checked until the
    scraper exposes dedicated page extractors.  The UI can still disclose the
    actual GBP values without inventing a page comparison.
    """
    profile = build_gbp_profile(context)
    if not profile:
        return []
    business = context.get("business") if isinstance(context.get("business"), dict) else {}
    url = _optional_str(context.get("url"))
    rows = [
        _alignment_row("name", "Business name", business.get("name"), profile.get("name"), ["entity_presence", "entity_consistency"]),
        _alignment_row("phone", "Phone", business.get("phone"), profile.get("phone"), ["entity_presence", "entity_consistency"]),
        _alignment_row("address", "Address", None, profile.get("address"), ["entity_presence", "entity_consistency"]),
        _alignment_row("website", "Website", url, profile.get("website"), ["entity_presence", "entity_consistency"]),
        _alignment_row("service_area", "Service area", None, ", ".join(profile.get("service_areas") or []) or None, ["real_world_connection"]),
    ]
    return rows


def _alignment_row(
    field_key: str,
    field_label: str,
    page_value: Any,
    gbp_value: Any,
    related_layer_keys: list[str],
) -> dict[str, Any]:
    page_text = _optional_str(page_value)
    gbp_text = _optional_str(gbp_value)
    comparable = field_key in {"name", "phone", "website"}
    if not comparable or not page_text or not gbp_text:
        status = "not_checked"
    elif _comparison_key(field_key, page_text) == _comparison_key(field_key, gbp_text):
        status = "match"
    else:
        status = "mismatch"
    if status == "match":
        impact = "The checked page signal agrees with the verified GBP record."
        suggested_fix = "Keep this value consistent across visible page templates and structured data."
    elif status == "mismatch":
        impact = "Different values can make it harder to connect the page to one stable business entity."
        suggested_fix = "Confirm the canonical value, then update the page and GBP record where appropriate."
    else:
        impact = "The current scraper did not extract a comparable page value for this field."
        suggested_fix = "Review this field manually before treating it as aligned or mismatched."
    return {
        "field_key": field_key,
        "field_label": field_label,
        "page_value": page_text,
        "gbp_value": gbp_text,
        "status": status,
        "impact": impact,
        "suggested_fix": suggested_fix,
        "related_layer_keys": related_layer_keys,
    }


def _comparison_key(field_key: str, value: str) -> str:
    if field_key == "phone":
        return "".join(char for char in value if char.isdigit())
    if field_key == "website":
        parsed = urlparse(value if "://" in value else f"https://{value}")
        return parsed.netloc.lower().removeprefix("www.")
    return "".join(char for char in value.casefold() if char.isalnum())


def _sub_page_paths(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [urlparse(str(url)).path.lower() for url in value if _optional_str(url)]


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [text for item in value if (text := _optional_str(item))]
    text = _optional_str(value)
    return [text] if text else []


def _has_usable_gbp_data(value: Any) -> bool:
    if not isinstance(value, dict) or not value:
        return False
    useful_keys = ("name", "address", "phone", "rating", "reviews", "website", "data_id", "review_list")
    return any(bool(value.get(key)) for key in useful_keys)


def _has_reviews(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    reviews = value.get("review_list")
    return isinstance(reviews, list) and len(reviews) > 0


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value).strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result
