from app.report_v22.evidence_adapters.competitor import observations
from evidence_helpers import competitor_source


def test_all_competitor_evidence_is_identity_scoped_and_reviews_are_traceable():
    items=list(observations(competitor_source()))
    assert all(i.selector.competitor_id for i in items)
    assert {i.source_type for i in items} == {"competitor","coverage"}
    replies=[i for i in items if i.selector.field == "owner_response_text"]
    assert len(replies)==3
    assert all(i.normalized_value == "Thank you for your feedback." for i in replies)
    assert all("/reviews/0/owner_response_text" in i.origin_paths[0] for i in replies)
    assert any(i.gap_reason == "partial" for i in items)


def test_nested_site_uses_collection_snapshot_and_physical_path():
    from app.collectors.site_inventory_models import SiteInventorySnapshot
    from app.report_v22.evidence import build_evidence_index
    from evidence_helpers import deep_site_source, serp_source, bind, build_input
    market=serp_source()
    source=competitor_source(market)
    nested=SiteInventorySnapshot.model_validate_json(deep_site_source().payload.model_dump_json().replace("example.test","competitor-1.test"))
    item=source.payload.competitors[0]
    item.site_inventory=nested
    item.site_status="available"
    item.analyzed_page_count=1
    source.binding=bind(source.payload,"competitor",13)
    result=build_evidence_index(build_input(market,source))
    fragment=next(t for t in result.source_traces if t.selector.category=="page_fragment")
    assert fragment.snapshot_id==source.binding.snapshot_id
    assert fragment.selector.competitor_id=="cp_competitor_1"
    assert fragment.origin_paths==["/payload/competitors/0/site_inventory/selected_pages/0/deep_snapshot/text"]
