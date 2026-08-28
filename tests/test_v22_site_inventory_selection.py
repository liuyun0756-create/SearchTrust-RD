from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest

from app.collectors.site_inventory_html import HtmlStructure
from app.collectors.site_inventory_models import GscPagePriority, InventoryPageRecord
from app.collectors.site_inventory_selection import (
    classify_page,
    gsc_priorities_from_snapshots,
    select_key_pages,
)
from app.collectors.site_inventory_urls import SiteScope
from app.api.v2.models import FirstPartySnapshotEnvelope


def structure(*, title: str = "", h1: str = "", schema_types: tuple[str, ...] = ()) -> HtmlStructure:
    return HtmlStructure(
        title=title or None,
        h1=h1 or None,
        canonical_url=None,
        meta_robots=(),
        schema_types=schema_types,
        internal_links=(),
        visible_text="",
    )


@pytest.mark.parametrize(
    ("url", "title", "schema_types", "expected"),
    [
        ("https://example.com/", "Example", (), "home"),
        ("https://example.com/services", "Our Services", (), "service_index"),
        ("https://example.com/services/plumbing", "Plumbing", (), "service_detail"),
        ("https://example.com/service-areas/austin", "Austin Plumber", (), "service_area"),
        ("https://example.com/locations/downtown", "Downtown Office", (), "location"),
        ("https://example.com/about", "About Us", (), "about"),
        ("https://example.com/contact", "Contact", (), "contact"),
        ("https://example.com/team", "Our Team", (), "team"),
        ("https://example.com/reviews", "Testimonials", (), "review_testimonial"),
        ("https://example.com/case-studies/acme", "Acme Project", (), "case_study_portfolio"),
        ("https://example.com/questions", "Common Questions", ("FAQPage",), "faq"),
        ("https://example.com/blog", "Blog", (), "blog_index"),
        ("https://example.com/blog/how-to-fix", "How to Fix", ("BlogPosting",), "blog_post"),
        ("https://example.com/products", "Products", (), "product_category"),
        ("https://example.com/items/widget", "Widget", ("Product",), "product_detail"),
        ("https://example.com/privacy", "Privacy Policy", (), "legal"),
        ("https://example.com/company/history", "Our History", (), "other"),
    ],
)
def test_classifies_fixed_page_types(
    url: str,
    title: str,
    schema_types: tuple[str, ...],
    expected: str,
) -> None:
    result = classify_page(
        url=url,
        root_url="https://example.com/",
        structure=structure(title=title, h1=title, schema_types=schema_types),
    )

    assert result.page_type == expected
    assert result.reasons


def test_classification_is_deterministic() -> None:
    page = structure(title="Emergency Plumbing", schema_types=("Service",))

    first = classify_page(
        url="https://example.com/services/emergency",
        root_url="https://example.com/",
        structure=page,
    )
    second = classify_page(
        url="https://example.com/services/emergency",
        root_url="https://example.com/",
        structure=page,
    )

    assert first == second


NOW = datetime(2026, 8, 28, 8, 0, tzinfo=timezone.utc)


def checked_page(url: str, page_type: str, *, title: str = "") -> InventoryPageRecord:
    return InventoryPageRecord(
        url=url,
        discovery_sources=["internal_link" if page_type != "home" else "seed"],
        crawl_depth=0 if page_type == "home" else 1,
        check_status="checked",
        status_code=200,
        final_url=url,
        content_type="text/html",
        response_bytes=100,
        title=title or page_type,
        h1=title or page_type,
        canonical_url=url,
        meta_robots=[],
        schema_types=[],
        internal_links=[],
        page_type=page_type,
        classification_reasons=["fixture"],
    )


def test_selection_prioritizes_home_business_match_and_type_coverage() -> None:
    pages = [
        checked_page("https://example.com/", "home"),
        checked_page("https://example.com/blog/post", "blog_post", title="Austin Plumbing Tips"),
        checked_page("https://example.com/services/plumbing", "service_detail", title="Austin Plumbing"),
        checked_page("https://example.com/about", "about"),
        checked_page("https://example.com/contact", "contact"),
    ]

    ranked = select_key_pages(
        pages,
        limit=4,
        primary_service="plumbing",
        target_market="Austin",
        gsc_priorities=[],
    )

    assert [item.page.page_type for item in ranked] == [
        "home",
        "service_detail",
        "about",
        "contact",
    ]


def test_gsc_priority_changes_order_within_same_page_type() -> None:
    pages = [
        checked_page("https://example.com/", "home"),
        checked_page("https://example.com/services/a", "service_detail", title="Plumbing A"),
        checked_page("https://example.com/services/b", "service_detail", title="Plumbing B"),
    ]

    ranked = select_key_pages(
        pages,
        limit=2,
        primary_service="plumbing",
        target_market="Austin",
        gsc_priorities=[
            GscPagePriority(
                url="https://example.com/services/b",
                impressions=1000,
                clicks=100,
                observed_at=NOW,
            )
        ],
    )

    assert str(ranked[1].page.url) == "https://example.com/services/b"
    assert "gsc_priority" in ranked[1].reasons


def test_selection_excludes_noindex_and_caps_blog_posts() -> None:
    pages = [checked_page("https://example.com/", "home")]
    pages.extend(
        checked_page(f"https://example.com/blog/{index}", "blog_post") for index in range(10)
    )
    hidden = checked_page("https://example.com/hidden", "service_detail").model_copy(
        update={"meta_robots": ["noindex"]}
    )
    pages.append(hidden)

    ranked = select_key_pages(
        pages,
        limit=5,
        primary_service="plumbing",
        target_market="Austin",
        gsc_priorities=[],
    )

    assert sum(item.page.page_type == "blog_post" for item in ranked) <= 2
    assert all(str(item.page.url) != "https://example.com/hidden" for item in ranked)


def gsc_snapshot(*, health: str = "healthy", expires_at: datetime | None = None) -> FirstPartySnapshotEnvelope:
    return FirstPartySnapshotEnvelope.model_validate(
        {
            "snapshot_id": UUID("33333333-3333-4333-8333-333333333333"),
            "source_type": "gsc",
            "schema_version": "gsc_v1",
            "fetched_at": NOW,
            "expires_at": expires_at,
            "identity_match_status": "matched",
            "health_status": health,
            "health_reasons": [],
            "normalized_payload": {
                "rows": [
                    {
                        "dimensions": [{"key": "page", "value": "https://www.example.com/service/"}],
                        "metrics": [
                            {"key": "impressions", "value": 25.0, "unit": "count"},
                            {"key": "clicks", "value": 5.0, "unit": "count"},
                        ],
                    },
                    {
                        "dimensions": [{"key": "page", "value": "https://other.example/page"}],
                        "metrics": [{"key": "impressions", "value": 999.0, "unit": "count"}],
                    },
                ],
                "aggregates": [],
                "limitations": [],
            },
            "provider_request_context": {"external_resource_id": "sc-domain:example.com"},
            "payload_checksum": f"sha256:{'a' * 64}",
        }
    )


def test_extracts_only_healthy_current_same_site_gsc_rows() -> None:
    priorities = gsc_priorities_from_snapshots(
        [gsc_snapshot(expires_at=NOW + timedelta(days=1))],
        scope=SiteScope.from_root("https://example.com/"),
        now=NOW,
    )

    assert len(priorities) == 1
    assert str(priorities[0].url) == "https://example.com/service"
    assert priorities[0].impressions == 25
    assert priorities[0].clicks == 5

    assert gsc_priorities_from_snapshots(
        [gsc_snapshot(health="unhealthy")],
        scope=SiteScope.from_root("https://example.com/"),
        now=NOW,
    ) == []
