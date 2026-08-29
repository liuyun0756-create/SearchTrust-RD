"""Pure normalization for public competitor place profiles and reviews."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.competitors_v22.models import PublicGbpProfile, PublicReviewRecord
from app.jobs_v22.digest import request_digest


def bounded_text(value: Any, limit: int) -> str | None:
    text = str(value or "").strip()
    return text if text and len(text) <= limit else None


def request_record_id(params: dict[str, str]) -> str:
    return f"req_{request_digest(params)[7:31]}"


def sanitize_public_profile_payload(payload: dict[str, Any], engine: str) -> dict[str, Any]:
    """Discard contributor identity, photos, arbitrary links, and unknown provider fields."""

    if engine == "google_maps_reviews":
        clean_reviews: list[dict[str, Any]] = []
        raw_reviews = payload.get("reviews")
        for raw in raw_reviews[:8] if isinstance(raw_reviews, list) else []:
            if not isinstance(raw, dict):
                continue
            clean = {
                key: value
                for key, limit in (
                    ("review_id", 500),
                    ("iso_date", 120),
                    ("date", 120),
                    ("snippet", 10_000),
                    ("text", 10_000),
                )
                if (value := bounded_text(raw.get(key), limit)) is not None
            }
            if isinstance(raw.get("rating"), (int, float)) and not isinstance(raw.get("rating"), bool):
                clean["rating"] = raw["rating"]
            response = raw.get("response")
            if isinstance(response, dict):
                clean_response = {
                    key: value
                    for key, limit in (("iso_date", 120), ("snippet", 10_000), ("text", 10_000))
                    if (value := bounded_text(response.get(key), limit)) is not None
                }
                if clean_response:
                    clean["response"] = clean_response
            clean_reviews.append(clean)
        clean_payload: dict[str, Any] = {"reviews": clean_reviews}
        pagination = payload.get("serpapi_pagination")
        token = bounded_text(pagination.get("next_page_token"), 500) if isinstance(pagination, dict) else None
        if token:
            clean_payload["serpapi_pagination"] = {"next_page_token": token}
        return clean_payload

    raw = payload.get("place_results") or payload.get("local_results")
    if isinstance(raw, list):
        raw = raw[0] if raw else None
    if not isinstance(raw, dict):
        return {"place_results": {}}
    clean_place: dict[str, Any] = {}
    for key, limit in (
        ("title", 240), ("name", 240), ("website", 2083), ("address", 500),
        ("place_url", 2083), ("link", 2083), ("place_id", 500), ("data_id", 500),
        ("cid", 200), ("type", 240),
    ):
        if (value := bounded_text(raw.get(key), limit)) is not None:
            clean_place[key] = value
    for key in ("rating", "reviews", "reviews_count"):
        value = raw.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            clean_place[key] = value
    for key in ("types", "categories"):
        values = raw.get(key)
        if isinstance(values, list):
            clean_place[key] = [
                item for value in values[:20] if (item := bounded_text(value, 240)) is not None
            ]
    return {"place_results": clean_place}


def _date(value: Any) -> datetime | None:
    text = bounded_text(value, 120)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def normalize_place_profile(
    payload: dict[str, Any], *, collected_at: datetime, params: dict[str, str]
) -> PublicGbpProfile | None:
    raw = payload.get("place_results") or payload.get("local_results")
    if isinstance(raw, list):
        raw = raw[0] if raw else None
    if not isinstance(raw, dict):
        return None
    name = bounded_text(raw.get("title") or raw.get("name"), 240)
    if not name:
        return None
    categories_raw = raw.get("types") or raw.get("categories") or raw.get("type") or []
    categories_values = categories_raw if isinstance(categories_raw, list) else [categories_raw]
    categories = list(
        dict.fromkeys(
            value
            for item in categories_values[:20]
            if (value := bounded_text(item, 240)) is not None
        )
    )
    rating_raw = raw.get("rating")
    rating = float(rating_raw) if isinstance(rating_raw, (int, float)) and not isinstance(rating_raw, bool) else None
    reviews_raw = raw.get("reviews") or raw.get("reviews_count")
    review_count = reviews_raw if isinstance(reviews_raw, int) and not isinstance(reviews_raw, bool) else None
    return PublicGbpProfile(
        business_name=name,
        public_gbp_url=bounded_text(raw.get("place_url") or raw.get("link"), 2083),
        provider_place_id=bounded_text(raw.get("place_id"), 500),
        provider_data_id=bounded_text(raw.get("data_id"), 500),
        provider_cid=bounded_text(raw.get("cid"), 200),
        website_url=bounded_text(raw.get("website"), 2083),
        address=bounded_text(raw.get("address"), 500),
        categories=categories,
        rating=rating,
        review_count=review_count,
        collected_at=collected_at,
        request_record_id=request_record_id(params),
    )


def normalize_review_page(
    payload: dict[str, Any], *, collected_at: datetime, params: dict[str, str]
) -> tuple[list[PublicReviewRecord], str | None]:
    raw_reviews = payload.get("reviews")
    if not isinstance(raw_reviews, list):
        raw_reviews = []
    records: list[PublicReviewRecord] = []
    for raw in raw_reviews[:8]:
        if not isinstance(raw, dict):
            continue
        rating_raw = raw.get("rating")
        if not isinstance(rating_raw, (int, float)) or isinstance(rating_raw, bool):
            continue
        if not 0 <= float(rating_raw) <= 5:
            continue
        iso_date = _date(raw.get("iso_date"))
        text = bounded_text(raw.get("snippet") or raw.get("text"), 10_000)
        provider_id = bounded_text(raw.get("review_id"), 500)
        fallback = request_digest(
            {"rating": float(rating_raw), "iso_date": iso_date.isoformat() if iso_date else None, "text": text}
        )[7:31]
        response = raw.get("response") if isinstance(raw.get("response"), dict) else {}
        response_text = bounded_text(response.get("snippet") or response.get("text"), 10_000)
        stable_id = request_digest(
            {"provider_review_id": provider_id, "fallback": fallback}
        )[7:31]
        records.append(
            PublicReviewRecord(
                review_record_id=f"rv_{stable_id}",
                provider_review_id=provider_id,
                rating=float(rating_raw),
                iso_date=iso_date,
                original_date_text=bounded_text(raw.get("date"), 120),
                text=text,
                owner_response_text=response_text,
                owner_response_iso_date=_date(response.get("iso_date")) if response_text else None,
                source="google_maps_reviews",
                collected_at=collected_at,
                request_record_id=request_record_id(params),
            )
        )
    pagination = payload.get("serpapi_pagination")
    token = bounded_text(pagination.get("next_page_token"), 500) if isinstance(pagination, dict) else None
    return records, token
