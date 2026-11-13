"""A bounded, new preparation view; persisted collection checkpoints stay intact."""
from dataclasses import replace

from app.collectors.site_inventory_models import SiteInventorySnapshotV2, SiteInventorySnapshotV3
from app.jobs_v22.digest import canonical_json_bytes


RESOURCE_LIMIT = "resource_limit"
SIZE_LIMIT_NOTE = "Some page content was not checked because the saved analysis input has a size limit."


def bound_customer_html(collected, *, max_html_bytes):
    """Follow the existing P0-10 page-order/HTML budget on a derived snapshot.

    Called only when the complete generation input would otherwise be rejected.
    Retain structural observations and the real fetch-attempt history. Only the
    deep content admitted for analysis changes, with an explicit unknown marker.
    The caller rebuilds all source bindings/checksums before freezing anything.
    """
    site = collected.site_inventory
    if not isinstance(site, (SiteInventorySnapshotV2, SiteInventorySnapshotV3)):
        return collected
    selected = {str(page.url): page for page in site.selected_pages}
    omitted, used, exhausted = set(), 0, False
    for page in site.pages:
        chosen = selected.get(str(page.url))
        if chosen is None or chosen.deep_snapshot is None:
            continue
        size = len(chosen.deep_snapshot.html.encode("utf-8"))
        if exhausted or used + size > max_html_bytes:
            exhausted = True
            omitted.add(str(page.url))
        else:
            used += size
    if not omitted:
        return collected
    pages = [page.model_copy(update={
        "deep_analyzed": False, "deep_snapshot": None,
        "selection_reasons": list(dict.fromkeys([*page.selection_reasons, RESOURCE_LIMIT])),
    }) if str(page.url) in omitted else page for page in site.selected_pages]
    limited = site.model_copy(update={
        "selected_pages": pages,
        "deep_analyzed_count": sum(page.deep_analyzed for page in pages),
        "limitations": list(dict.fromkeys([*site.limitations, SIZE_LIMIT_NOTE])),
    })
    # Revalidate derived counts and existing model bounds. Never mutate the raw
    # snapshot, write its checkpoint, retimestamp evidence, or truncate HTML.
    type(site).model_validate_json(canonical_json_bytes(limited))
    # Validation may normalize whitespace. Return the derived copy containing
    # the original, already-validated retained evidence, never normalized HTML.
    return replace(collected, site_inventory=limited)
