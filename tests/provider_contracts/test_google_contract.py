from datetime import date

import pytest

from app.google_connections_v22.ga4 import normalize_report
from app.google_connections_v22.gbp import (
    normalize_keyword_page,
    normalize_location,
    normalize_performance,
)
from app.google_connections_v22.gsc import normalize_view
from app.google_connections_v22.gsc import SyncError


pytestmark = pytest.mark.contract


def test_gsc_fixture_matches_normalized_query_view(provider_fixture) -> None:
    result = normalize_view(
        provider_fixture("google/gsc_view.json"),
        dimension="query",
        limit=250,
        start=date(2026, 6, 1),
        end=date(2026, 9, 1),
    )
    assert result.rows[0].key == "fixture plumbing"
    assert result.rows[0].ctr == 0.1


def test_ga4_fixture_matches_normalized_landing_page_view(provider_fixture) -> None:
    result = normalize_report(
        provider_fixture("google/ga4_landing_page.json"),
        view="landing_pages",
        start=date(2026, 6, 1),
        end=date(2026, 9, 1),
    )
    assert result.rows[0].landing_page == "/fixture-service"
    assert result.rows[0].sessions == 10


def test_gbp_fixture_matches_normalized_profile_checks(provider_fixture) -> None:
    result = normalize_location(
        provider_fixture("google/gbp_location.json"),
        "locations/12345",
    )
    assert all(result.model_dump().values())

    performance = normalize_performance(
        provider_fixture("google/gbp_performance.json"),
        date(2026, 6, 1),
        date(2026, 9, 1),
    )
    keywords, next_page = normalize_keyword_page(
        provider_fixture("google/gbp_keywords.json"), set()
    )
    assert performance["WEBSITE_CLICKS"][date(2026, 9, 1)] == 4
    assert keywords[0].threshold == 10
    assert next_page == ""


@pytest.mark.parametrize("mutation", ["empty", "partial", "unknown", "oversized"])
def test_google_gsc_shape_drift_fails_closed(provider_fixture, mutation: str) -> None:
    payload = provider_fixture("google/gsc_view.json")
    if mutation == "empty":
        payload = {"rows": [], "responseAggregationType": "auto"}
    elif mutation == "partial":
        payload["rows"][0].pop("ctr")
    elif mutation == "unknown":
        payload["rows"][0]["unknown"] = True
    else:
        payload["rows"] = payload["rows"] * 252

    if mutation == "empty":
        assert normalize_view(
            payload, dimension="query", limit=250,
            start=date(2026, 6, 1), end=date(2026, 9, 1),
        ).rows == []
    else:
        with pytest.raises(SyncError, match="SYNC_INVALID_GOOGLE_RESPONSE"):
            normalize_view(
                payload, dimension="query", limit=250,
                start=date(2026, 6, 1), end=date(2026, 9, 1),
            )
