"""Small strict synthetic inputs for public rule boundary tests."""
from collections import Counter

from app.collectors.site_inventory_models import InventoryCount, SiteInventorySnapshot
from app.jobs_v22.digest import request_digest
from app.report_v22.findings_models import PublicFindingsInput
from evidence_helpers import build_input, site_source, serp_source, competitor_source, bind
from test_v22_site_inventory_models import page


def site(records=None, host="example.test"):
    source = site_source()
    payload = source.payload
    records = records if records is not None else [{}]
    pages = []
    for index, changes in enumerate(records):
        url = f"https://{host}/" + (f"page-{index}" if index else "")
        pages.append(page(url).model_copy(update=changes))
    payload.root_url = f"https://{host}/"
    payload.canonical_host = host
    payload.pages = pages
    payload.selected_pages = []
    payload.discovered_url_count = len(pages)
    payload.structurally_checked_count = sum(p.check_status == "checked" for p in pages)
    payload.page_type_counts = [InventoryCount(label=k, count=v) for k, v in Counter(p.page_type for p in pages if p.check_status == "checked").items()]
    payload.source_counts = [InventoryCount(label="seed", count=len(pages))]
    source.payload = SiteInventorySnapshot.model_validate(payload.model_dump(mode="python"))
    source.binding = bind(source.payload, "site", 11)
    return source


def seal(source):
    source.payload = type(source.payload).model_validate(source.payload.model_dump(mode="python"))
    source.binding.payload_checksum = request_digest(source.payload)
    source.binding.fetched_at = source.payload.completed_at
    return source


def market(entries=None):
    source = serp_source()
    entries = entries if entries is not None else [
        ("example.test", 10, "provider_position"),
        ("competitor-1.test", 1, "provider_position"),
        ("competitor-2.test", 2, "provider_position"),
        ("competitor-3.test", 3, "provider_position"),
    ]
    for run in source.payload.query_runs:
        template = run.results[0]
        run.results = [template.model_copy(update={
            "record_id": request_digest([run.query, i, domain]),
            "url": f"https://{domain}/" if domain else None,
            "normalized_domain": domain, "position": position, "rank_source": basis,
        }) for i, (domain, position, basis) in enumerate(entries)]
    source.payload.result_counts[0].count = len(entries) * 3
    return seal(source)


def collection(market_source, types=("service_detail", "service_detail", "home")):
    source = competitor_source(market_source)
    for i, (item, kind) in enumerate(zip(source.payload.competitors, types), 1):
        if kind is not None:
            item.site_inventory = site([{"page_type": kind}], host=f"competitor-{i}.test").payload
            item.site_status = "available"
    return seal(source)


def request(*sources, **context):
    return PublicFindingsInput(evidence_input=build_input(*sources, **context))
