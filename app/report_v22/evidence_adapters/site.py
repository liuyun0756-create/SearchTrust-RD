"""Adapt checked site inventory into deterministic V2.2 evidence."""

from app.report_common.page_fragments import extract_page_fragments
from app.report_v22.evidence_adapters.common import coverage, fields, observe
from app.report_v22.models import SourceLocator
from app.report_v22.evidence_adapters.site_counts import observations as count_observations

TEXT_LIMITATION = "Page fragments use the existing extractor: at most 240 segments and 360 characters per segment; they are not the complete page."


def observations(source, *, inventory=None, prefix="/payload", competitor_id=None, limitations=()):
    inventory = inventory or source.payload
    notes = sorted(set([*limitations, *inventory.limitations]))
    root = SourceLocator(url=inventory.root_url)
    kwargs = dict(competitor_id=competitor_id, limitations=notes)
    yield from fields(source,"site_field",str(inventory.root_url),inventory,prefix,
        ("discovered_url_count","structurally_checked_count","deep_analyzed_count"),locator=root,**kwargs)
    for name in ("page_type_counts","source_counts"):
        for index, count in enumerate(getattr(inventory,name)):
            yield observe(source,"site_field",[str(inventory.root_url),name,count.label],name,count.count,
                f"{prefix}/{name}/{index}/count",locator=root,**kwargs)
    selected = {str(p.url):(i,p) for i,p in enumerate(inventory.selected_pages)}
    for index, page in enumerate(inventory.pages):
        url = str(page.url)
        path = f"{prefix}/pages/{index}"
        locator = SourceLocator(url=page.url)
        if page.check_status != "checked":
            yield coverage(source,url,"check_status","unavailable",page.check_status,f"{path}/check_status",
                locator=locator,health_status="unavailable",**kwargs)
            continue
        yield from fields(source,"site_field",url,page,path,
            ("check_status","status_code","final_url","content_type","response_bytes","title","h1","canonical_url","meta_robots","schema_types","page_type"),locator=locator,**kwargs)
        position, selected_page = selected.get(url,(None,None))
        if selected_page is None or selected_page.deep_snapshot is None:
            state_path = f"{prefix}/selected_pages/{position}/deep_analyzed" if selected_page is not None else f"{path}/check_status"
            yield coverage(source,url,"deep_snapshot","partial",False if selected_page else page.check_status,state_path,
                locator=locator,health_status="healthy",**kwargs)
            continue
        deep = selected_page.deep_snapshot
        deep_path = f"{prefix}/selected_pages/{position}/deep_snapshot"
        if not 200 <= deep.status_code < 300:
            yield coverage(source,url,"deep_status","unavailable",deep.status_code,f"{deep_path}/status_code",
                locator=locator,health_status="unavailable",**kwargs)
            continue
        # content_checksum is the upstream byte digest, NOT a digest of decoded HTML.
        fragments = extract_page_fragments(deep.text)
        if not fragments:
            yield coverage(source,url,"page_fragments","empty",None,f"{deep_path}/text",locator=locator,health_status="healthy",**kwargs)
        for fragment in fragments:
            # Extractor section/kind is reproducible from text; no old page-N ID.
            key = [url,fragment["page_section"],fragment["kind"],fragment["text"]]
            yield observe(source,"page_fragment",key,"text",fragment["text"],f"{deep_path}/text",
                locator=locator,competitor_id=competitor_id,collected_at=deep.collected_at,limitations=[*notes,TEXT_LIMITATION])
    yield from count_observations(source, inventory, prefix, competitor_id, notes)
