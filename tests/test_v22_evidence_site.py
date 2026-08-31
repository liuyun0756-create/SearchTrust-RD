from app.report_v22.evidence_adapters.site import observations
from app.report_v22.evidence_identity import evidence_id
from evidence_helpers import site_source, deep_site_source


def test_structural_fields_and_no_invented_body():
    items = list(observations(site_source()))
    assert any(i.selector.field == "status_code" and i.normalized_value == 200 for i in items)
    assert not any(i.selector.category == "page_fragment" for i in items)
    assert any(i.gap_reason == "partial" for i in items)


def test_page_fragments_reuse_cleanup_not_old_sequence_id():
    source = deep_site_source("We repair plumbing systems for homes in Austin.\nWe repair plumbing systems for homes in Austin.")
    items = [i for i in observations(source) if i.selector.category == "page_fragment"]
    assert len(items) == 2
    assert evidence_id(items[0]) == evidence_id(items[1])
    assert items[0].original_value == "We repair plumbing systems for homes in Austin."
    assert items[0].origin_paths == ["/payload/selected_pages/0/deep_snapshot/text"]


def test_text_extraction_limits_are_disclosed():
    items=list(observations(deep_site_source("A"*400)))
    fragment=next(i for i in items if i.selector.category == "page_fragment")
    assert len(fragment.normalized_value) == 360
    assert any("360" in note for note in fragment.limitations)


def test_failed_or_robots_page_cannot_emit_stale_content():
    for status in ("failed","robots_disallowed"):
        source=site_source()
        page=source.payload.pages[0]
        page.check_status=status
        page.error_code="robots_disallowed" if status=="robots_disallowed" else "fetch_failed"
        page.title="Unusable stale title"
        items=list(observations(source))
        assert not any(i.selector.field=="title" for i in items)
        assert any(i.gap_reason=="unavailable" for i in items)


def test_segment_cap_is_retained_and_deep_byte_checksum_is_not_reencoded():
    from evidence_helpers import build_input
    from app.report_v22.evidence import build_evidence_index
    source=deep_site_source("\n".join(f"Unique plumbing service description number {i}." for i in range(245)))
    # Fixture upstream checksum deliberately differs from decoded HTML's digest.
    result=build_evidence_index(build_input(source))
    assert sum(t.selector.category=="page_fragment" for t in result.source_traces)==240
