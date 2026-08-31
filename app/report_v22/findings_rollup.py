"""Sample-scoped rollups: technical observations never invent semantic ratings."""
from collections import defaultdict

from app.collectors.site_inventory_models import SITE_PAGE_TYPE_ORDER
from app.report_v22.evidence_adapters.site_counts import eligible_html
from app.report_v22.findings_models import FindingsRollup, SamplingCounts
from app.report_v22.models import LayerAssessment, REQUIRED_LAYER_KEYS

LAYER_NOTE = "Not checked: semantic_rules_not_implemented in this public-rule batch. Technical and sampled market observations are not trust-layer ratings."


def layers():
    return [LayerAssessment(layer_key=key, status="not_checked", summary=LAYER_NOTE) for key in REQUIRED_LAYER_KEYS]


def build_rollups(view, outcomes):
    findings = [outcome.finding for outcome in outcomes if outcome.finding]
    base = dict(scope="sampled_site", site_url=view.context.site_url,
                finding_ids=sorted(f.finding_id for f in findings), evidence_ids=[],
                layers=layers(), limitations=["The site scope is this saved sample, not proof of complete website coverage."])
    if not view.available("site"):
        base["limitations"].append(f"Site sampling counts are unknown: {view.missing_reason('site')}.")
        return FindingsRollup(**base, counts=SamplingCounts()), []
    source = view.sources["site"]
    p = source.payload
    snapshot = source.binding.snapshot_id
    total, total_refs = view.count(source, "/payload")
    site_refs = view.field_refs(snapshot, "/payload", "discovered_url_count", "structurally_checked_count", "deep_analyzed_count")
    base["evidence_ids"] = sorted(set(total_refs + site_refs))
    base["limitations"] = sorted(set(base["limitations"] + view.summaries[snapshot].limitations))
    base["urls"] = sorted((page.url for page in p.pages if page.check_status == "checked"), key=str)
    site = FindingsRollup(**base, counts=SamplingCounts(discovered=p.discovered_url_count,
        checked=p.structurally_checked_count, eligible_html=total, deep_analyzed=p.deep_analyzed_count))
    grouped = defaultdict(list)
    for index, page in enumerate(p.pages):
        if page.check_status == "checked":
            grouped[page.page_type].append((index, page))
    deep = {str(page.url) for page in p.selected_pages if page.deep_analyzed}
    clusters = []
    for kind in SITE_PAGE_TYPE_ORDER:
        pages = grouped.get(kind, [])
        if not pages:
            continue
        urls = sorted((page.url for _, page in pages), key=str)
        url_set = {str(url) for url in urls}
        local_findings = [outcome.finding.finding_id for outcome in outcomes if outcome.finding
                          and outcome.evaluation.target.kind in {"page", "title_group"}
                          and url_set.intersection(str(url) for url in outcome.finding.affected_urls)]
        refs = [key for index, _ in pages for key in view.field_refs(snapshot, f"/payload/pages/{index}", "page_type", "status_code", "content_type")]
        clusters.append(FindingsRollup(scope="sampled_page_type", site_url=view.context.site_url, page_type=kind,
            urls=urls, counts=SamplingCounts(checked=len(pages), eligible_html=sum(eligible_html(page) for _, page in pages),
                                            deep_analyzed=sum(str(page.url) in deep for _, page in pages)),
            finding_ids=sorted(set(local_findings)), evidence_ids=sorted(set(refs)), layers=layers(),
            limitations=base["limitations"]))
    return site, clusters
