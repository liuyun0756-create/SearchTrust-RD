from datetime import timedelta

from app.competitors_v22.public_profile import normalize_place_profile, normalize_review_page
from test_v22_competitor_models import NOW


def test_place_profile_keeps_only_bounded_public_business_fields() -> None:
    params = {"engine": "google_maps", "type": "place", "place_id": "place-1"}
    profile = normalize_place_profile(
        {
            "place_results": {
                "title": "Alpha Plumbing",
                "website": "https://alpha.example/",
                "address": "1 Main St, Austin, TX",
                "types": ["Plumber"],
                "rating": 4.8,
                "reviews": 42,
                "place_id": "place-1",
                "data_id": "data-1",
                "cid": "123",
                "photos": [{"private": "discard"}],
            }
        },
        collected_at=NOW,
        params=params,
    )

    assert profile is not None
    assert profile.business_name == "Alpha Plumbing"
    assert profile.review_count == 42
    assert "photos" not in profile.model_dump()


def test_reviews_are_capped_to_eight_and_only_follow_page_token() -> None:
    reviews = [
        {
            "review_id": f"review-{index}",
            "rating": 5,
            "iso_date": (NOW - timedelta(days=index)).isoformat(),
            "snippet": f"Review {index}",
            "user": {"profile": "discard"},
        }
        for index in range(10)
    ]
    records, token = normalize_review_page(
        {
            "reviews": reviews,
            "serpapi_pagination": {
                "next_page_token": "token-2",
                "next": "https://untrusted.example/next",
            },
        },
        collected_at=NOW,
        params={"engine": "google_maps_reviews", "data_id": "data-1"},
    )

    assert len(records) == 8
    assert token == "token-2"
    assert len({record.review_record_id for record in records}) == 8
    assert "user" not in records[0].model_dump()
