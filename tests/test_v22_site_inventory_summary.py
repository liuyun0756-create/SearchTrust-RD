from datetime import datetime, timezone

from pydantic import ValidationError
import pytest

from app.collectors.site_inventory_models import (
    InventoryCount,
    InventoryPageRecord,
    SelectedPageRecord,
    SiteInventorySnapshot,
)
from app.collectors.site_inventory_summary import to_site_inventory_summary


NOW = datetime(2026, 8, 28, 8, 0, tzinfo=timezone.utc)


def inventory() -> SiteInventorySnapshot:
    page = InventoryPageRecord(
        url="https://example.com/",
        discovery_sources=["seed"],
        crawl_depth=0,
        check_status="checked",
        status_code=200,
        final_url="https://example.com/",
        content_type="text/html",
        response_bytes=20,
        title="Example",
        h1="Example",
        canonical_url="https://example.com/",
        meta_robots=[],
        schema_types=[],
        internal_links=[],
        page_type="home",
        classification_reasons=["root_url"],
    )
    return SiteInventorySnapshot(
        schema_version="site_inventory_snapshot_v1",
        root_url="https://example.com/",
        canonical_host="example.com",
        started_at=NOW,
        completed_at=NOW,
        discovery_limit=500,
        deep_analysis_limit=50,
        discovered_url_count=1,
        structurally_checked_count=1,
        deep_analyzed_count=0,
        pages=[page],
        selected_pages=[
            SelectedPageRecord(
                url="https://example.com/",
                page_type="home",
                crawl_depth=0,
                selection_score=100,
                selection_reasons=["home_required"],
                deep_analyzed=False,
                deep_snapshot=None,
            )
        ],
        deep_attempts=[],
        page_type_counts=[InventoryCount(label="home", count=1)],
        source_counts=[InventoryCount(label="seed", count=1)],
        limitations=["candidate_exhausted"],
    )


def test_maps_snapshot_to_frozen_summary() -> None:
    summary = to_site_inventory_summary(
        inventory(),
        evidence_ids_by_url={"https://example.com/": ["ev_site_home"]},
    )

    assert summary.discovered_url_count == 1
    assert summary.structurally_checked_count == 1
    assert summary.deep_analyzed_count == 0
    assert summary.selected_pages[0].evidence_ids == ["ev_site_home"]
    assert summary.limitations == ["candidate_exhausted"]


def test_summary_mapping_rejects_invalid_evidence_ids() -> None:
    with pytest.raises(ValidationError):
        to_site_inventory_summary(
            inventory(),
            evidence_ids_by_url={"https://example.com/": ["not-valid"]},
        )
