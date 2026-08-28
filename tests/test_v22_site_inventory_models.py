from datetime import datetime, timezone

from pydantic import ValidationError
import pytest

from app.collectors.site_inventory_models import (
    GscPagePriority,
    InventoryCount,
    InventoryPageRecord,
    SelectedPageRecord,
    SiteInventorySnapshot,
)


NOW = datetime(2026, 8, 28, 8, 0, tzinfo=timezone.utc)


def page(url: str = "https://example.com/") -> InventoryPageRecord:
    return InventoryPageRecord(
        url=url,
        discovery_sources=["seed"],
        crawl_depth=0,
        check_status="checked",
        status_code=200,
        final_url=url,
        content_type="text/html",
        response_bytes=120,
        title="Example",
        h1="Example",
        canonical_url=url,
        meta_robots=[],
        schema_types=["Organization"],
        internal_links=[],
        page_type="home",
        classification_reasons=["root_url"],
    )


def selected(url: str = "https://example.com/") -> SelectedPageRecord:
    return SelectedPageRecord(
        url=url,
        page_type="home",
        crawl_depth=0,
        selection_score=100,
        selection_reasons=["home_required"],
        deep_analyzed=False,
        deep_snapshot=None,
    )


def snapshot(**overrides: object) -> SiteInventorySnapshot:
    values = {
        "schema_version": "site_inventory_snapshot_v1",
        "root_url": "https://example.com/",
        "canonical_host": "example.com",
        "started_at": NOW,
        "completed_at": NOW,
        "discovery_limit": 500,
        "deep_analysis_limit": 50,
        "discovered_url_count": 1,
        "structurally_checked_count": 1,
        "deep_analyzed_count": 0,
        "pages": [page()],
        "selected_pages": [selected()],
        "deep_attempts": [],
        "page_type_counts": [InventoryCount(label="home", count=1)],
        "source_counts": [InventoryCount(label="seed", count=1)],
        "limitations": [],
    }
    values.update(overrides)
    return SiteInventorySnapshot.model_validate(values)


def test_snapshot_accepts_consistent_counts() -> None:
    result = snapshot()

    assert result.discovered_url_count == 1
    assert result.structurally_checked_count == 1
    assert result.deep_analyzed_count == 0


@pytest.mark.parametrize(
    "overrides",
    [
        {"discovered_url_count": 2},
        {"structurally_checked_count": 0},
        {"deep_analyzed_count": 1},
        {"page_type_counts": [InventoryCount(label="home", count=2)]},
    ],
)
def test_snapshot_rejects_inconsistent_counts(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        snapshot(**overrides)


def test_snapshot_rejects_duplicate_urls() -> None:
    duplicate = page()

    with pytest.raises(ValidationError, match="unique"):
        snapshot(
            discovered_url_count=2,
            structurally_checked_count=2,
            pages=[page(), duplicate],
            page_type_counts=[InventoryCount(label="home", count=2)],
        )


def test_models_reject_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        GscPagePriority(
            url="https://example.com/service",
            impressions=10,
            clicks=2,
            observed_at=NOW,
            secret="not-allowed",
        )


def test_page_collection_hard_limit_is_500() -> None:
    pages = [page(f"https://example.com/{index}") for index in range(501)]

    with pytest.raises(ValidationError):
        snapshot(
            discovered_url_count=501,
            structurally_checked_count=501,
            pages=pages,
            selected_pages=[],
            page_type_counts=[InventoryCount(label="home", count=501)],
        )


def test_selected_page_requires_matching_deep_snapshot_state() -> None:
    with pytest.raises(ValidationError):
        SelectedPageRecord(
            url="https://example.com/",
            page_type="home",
            crawl_depth=0,
            selection_score=100,
            selection_reasons=["home_required"],
            deep_analyzed=True,
            deep_snapshot=None,
        )
