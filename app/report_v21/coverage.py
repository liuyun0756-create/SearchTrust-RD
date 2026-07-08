"""Backend-owned coverage and GBP status helpers for report_v2_1."""

from __future__ import annotations

from typing import Any


def build_gbp_status(context: dict[str, Any]) -> dict[str, str | None]:
    """Build backend-owned GBP status from scraper context."""
    gbp_url = _optional_str(context.get("gbp_url"))
    gbp_error = _optional_str(context.get("gbp_error"))
    gbp_data = context.get("gbp_data")
    lookup_attempted = bool(context.get("gbp_lookup_attempted"))

    if gbp_error:
        return {
            "status": "error",
            "gbp_url": gbp_url,
            "reason": f"GBP lookup failed: {gbp_error}",
        }

    if _has_usable_gbp_data(gbp_data):
        return {
            "status": "checked",
            "gbp_url": gbp_url,
            "reason": None,
        }

    if gbp_url:
        return {
            "status": "not_found",
            "gbp_url": gbp_url,
            "reason": (
                "GBP lookup appears to have been attempted, but no confident "
                "usable GBP profile was returned. Current scraper output does "
                "not expose a more specific no-match reason."
            ),
        }

    if lookup_attempted:
        return {
            "status": "not_found",
            "gbp_url": gbp_url,
            "reason": (
                "GBP lookup was attempted from extracted business information, "
                "but no confident usable GBP profile was returned."
            ),
        }

    return {
        "status": "not_checked",
        "gbp_url": gbp_url,
        "reason": "GBP lookup was not attempted because no GBP URL or usable lookup input was available.",
    }


def build_data_coverage(
    context: dict[str, Any],
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    """Build backend-owned data coverage from scraper context."""
    warnings = list(warnings or [])
    gbp_status = build_gbp_status(context)
    gbp_data = context.get("gbp_data")
    sub_pages = context.get("sub_pages")

    page_content_checked = bool(context.get("content_checked"))
    gbp_checked = gbp_status["status"] == "checked"
    reviews_checked = _has_reviews(gbp_data)
    internal_pages_checked = isinstance(sub_pages, list) and len(sub_pages) > 0

    limitations = _dedupe([
        *warnings,
        *([] if page_content_checked else ["Page content was not confirmed as checked by the backend."]),
        "Schema extraction is not exposed by the current scraper.",
        "Contact page coverage is not separately classified by the current scraper.",
        "About page coverage is not separately classified by the current scraper.",
        *([] if reviews_checked else ["GBP reviews were not checked or no usable GBP reviews were returned."]),
        *([] if internal_pages_checked else ["Internal sub-page coverage was not confirmed by the scraper."]),
        "Competitor pages are not checked in v2.1 backend coverage.",
        *(
            []
            if gbp_checked
            else ["GBP alignment could not be verified because usable GBP data was not checked."]
        ),
    ])

    return {
        "page_content_checked": page_content_checked,
        "gbp_checked": gbp_checked,
        "schema_checked": False,
        "contact_page_checked": False,
        "about_page_checked": False,
        "reviews_checked": reviews_checked,
        "internal_pages_checked": internal_pages_checked,
        "competitor_pages_checked": False,
        "limitations": limitations,
    }


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
