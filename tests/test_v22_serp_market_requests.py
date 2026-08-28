from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.collectors.serp_market_models import SerpTargetPoint
from app.collectors.serp_market_requests import (
    SerpSearchPlanError,
    build_serp_search_plan,
    request_search_context,
)


NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)


def point() -> SerpTargetPoint:
    return SerpTargetPoint(
        requested_label="Austin, TX",
        canonical_name="Austin, TX",
        country_code="US",
        latitude=30.2672,
        longitude=-97.7431,
        source="explicit_coordinates",
        resolved_at=NOW,
    )


@pytest.mark.parametrize(
    ("queries", "expected_calls"),
    [
        (["one", "two", "three"], 6),
        (["one", "two", "three", "four"], 8),
        (["one", "two", "three", "four", "five"], 10),
    ],
)
def test_plan_has_exactly_two_ordered_calls_per_query(
    queries: list[str],
    expected_calls: int,
) -> None:
    plan = build_serp_search_plan(queries=queries, target_point=point())

    assert len(plan.calls) == expected_calls
    assert [(call.query, call.engine) for call in plan.calls[:4]] == [
        (queries[0], "google_maps"),
        (queries[0], "google"),
        (queries[1], "google_maps"),
        (queries[1], "google"),
    ]


def test_plan_preserves_query_and_uses_one_comparable_context() -> None:
    plan = build_serp_search_plan(
        queries=["  Emergency Plumber  ", "24 hour plumber", "water heater repair"],
        target_point=point(),
        device="mobile",
        language="en",
    )

    maps, google = plan.calls[:2]
    assert plan.queries[0] == "Emergency Plumber"
    assert maps.params == {
        "engine": "google_maps",
        "q": "Emergency Plumber",
        "type": "search",
        "ll": "@30.2672,-97.7431,14z",
        "gl": "us",
        "hl": "en",
        "device": "mobile",
    }
    assert google.params == {
        "engine": "google",
        "q": "Emergency Plumber",
        "lat": "30.2672",
        "lon": "-97.7431",
        "gl": "us",
        "hl": "en",
        "device": "mobile",
        "num": "20",
    }
    assert all("start" not in call.params and "api_key" not in call.params for call in plan.calls)


def test_plan_rejects_duplicate_and_out_of_range_queries() -> None:
    with pytest.raises(SerpSearchPlanError):
        build_serp_search_plan(queries=["One", "one", "three"], target_point=point())
    with pytest.raises(SerpSearchPlanError):
        build_serp_search_plan(queries=["one", "two"], target_point=point())
    with pytest.raises(SerpSearchPlanError):
        build_serp_search_plan(
            queries=["one", "two", "three", "four", "five", "six"],
            target_point=point(),
        )


def test_request_digest_changes_with_search_context() -> None:
    mobile = build_serp_search_plan(
        queries=["one", "two", "three"],
        target_point=point(),
        device="mobile",
    )
    desktop = build_serp_search_plan(
        queries=["one", "two", "three"],
        target_point=point(),
        device="desktop",
    )

    assert mobile.calls[0].request_digest != desktop.calls[0].request_digest


def test_request_context_uses_defaults_or_frozen_parent_values() -> None:
    assert request_search_context(SimpleNamespace(parent_report=None)) == ("mobile", "en")
    request = SimpleNamespace(
        parent_report=SimpleNamespace(
            case_context=SimpleNamespace(search_device="desktop", search_language="es")
        )
    )

    assert request_search_context(request) == ("desktop", "es")
