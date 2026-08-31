"""Auditable counts of the existing HTML sample, never full-site inventory claims."""
from collections import Counter

from app.collectors.site_inventory_models import InventoryPageRecord
from app.report_v22.evidence_adapters.common import observe
from app.report_v22.models import SourceLocator

COUNT_VERSION = "eligible_html_counts_v1"
ASSET_PAGE_TYPES = ("service_detail", "service_area", "location")


def eligible_html(page: InventoryPageRecord) -> bool:
    media_type = (page.content_type or "").split(";", 1)[0].strip().lower()
    return (page.check_status == "checked" and page.status_code is not None
            and 200 <= page.status_code < 300
            and media_type in {"text/html", "application/xhtml+xml"})


def observations(source, inventory, prefix, competitor_id, limitations):
    pages = [page for page in inventory.pages if eligible_html(page)]
    counts = Counter(page.page_type for page in pages)
    values = [("eligible_html_page_count", None, len(pages))]
    values.extend(("eligible_page_type_count", kind, counts[kind]) for kind in ASSET_PAGE_TYPES)
    for field, page_type, count in values:
        record = [str(inventory.root_url), field, page_type, COUNT_VERSION]
        observation = observe(
            source, "site_field", record, field, count, f"{prefix}/pages",
            locator=SourceLocator(url=inventory.root_url), competitor_id=competitor_id,
            limitations=limitations, collected_at=inventory.completed_at,
        )
        observation.origin_paths = [f"{prefix}/completed_at", f"{prefix}/pages"]
        yield observation
