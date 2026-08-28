"""Pure mapping from internal inventory snapshots to the frozen report contract."""

from __future__ import annotations

from app.collectors.site_inventory_models import SiteInventorySnapshot
from app.report_v22.models import LabelCount, SiteInventorySummary, SitePageSummary


def to_site_inventory_summary(
    snapshot: SiteInventorySnapshot,
    *,
    evidence_ids_by_url: dict[str, list[str]] | None = None,
) -> SiteInventorySummary:
    evidence = evidence_ids_by_url or {}
    return SiteInventorySummary(
        discovered_url_count=snapshot.discovered_url_count,
        structurally_checked_count=snapshot.structurally_checked_count,
        deep_analyzed_count=snapshot.deep_analyzed_count,
        discovery_limit=snapshot.discovery_limit,
        deep_analysis_limit=snapshot.deep_analysis_limit,
        page_type_counts=[
            LabelCount(label=item.label, count=item.count)
            for item in snapshot.page_type_counts
        ],
        selected_pages=[
            SitePageSummary(
                url=page.url,
                page_type=page.page_type,
                crawl_depth=page.crawl_depth,
                deep_analyzed=page.deep_analyzed,
                evidence_ids=evidence.get(str(page.url), []),
            )
            for page in snapshot.selected_pages
        ],
        limitations=list(snapshot.limitations),
    )
