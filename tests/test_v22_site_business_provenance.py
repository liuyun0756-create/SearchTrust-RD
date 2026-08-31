import json
import subprocess
import sys
from uuid import UUID

import pytest

from app.jobs_v22.digest import canonical_json_bytes, request_digest
from app.report_v22 import site_business_evidence as evidence
from app.report_v22.site_business_errors import SiteBusinessError
from app.report_v22.site_business_facts import build_site_business_facts
from site_business_helpers import business_html, request, two_pages


def test_saved_html_leading_whitespace_survives_revalidation_and_offsets():
    value = request(' \n中文\n<address>  一号 &amp; 二号  </address>\n ')
    raw_html = value.source.payload.selected_pages[0].deep_snapshot.html
    result = build_site_business_facts(value)
    origin = result.candidates[0].origins[0]
    assert raw_html[origin.start:origin.end] == origin.excerpt == '<address>  一号 &amp; 二号  </address>'
    assert origin.decoded_html_checksum == request_digest({"html": raw_html})
    assert result.candidates[0].scalar_value == "  一号 & 二号  "
    assert result.evidence_index[0].original_value == "  一号 & 二号  "


def test_script_pointer_escape_and_excerpt_are_actual_source_slices():
    html = business_html(name='引号 " slash / newline\n backslash \\ ', description="x" * 800)
    result = build_site_business_facts(request(html))
    for candidate in result.candidates:
        for origin in candidate.origins:
            assert origin.excerpt == html[origin.start:origin.start + 360]
            assert origin.excerpt_truncated is True
            document = json.loads(html[origin.start:origin.end])
            from app.report_v22.evidence import resolve_origin
            actual = resolve_origin(document, origin.json_pointer)
            assert actual == (candidate.components[origin.component] if origin.component else candidate.scalar_value)


def test_reordered_inventory_keeps_identity_and_input_is_not_mutated():
    value = two_pages()
    before = value.model_dump_json()
    original = build_site_business_facts(value)
    assert value.model_dump_json() == before
    value.source.payload.pages.reverse()
    value.source.payload.selected_pages.reverse()
    value.source.binding.payload_checksum = request_digest(value.source.payload)
    reordered = build_site_business_facts(value)
    assert [c.candidate_id for c in original.candidates] == [c.candidate_id for c in reordered.candidates]
    assert original.evidence_index == reordered.evidence_index
    assert original.source_traces != reordered.source_traces
    assert len(original.candidates) == 12  # Same values on two requests stay separate.


def test_actual_final_url_is_used_not_request_url():
    value = request()
    deep = value.source.payload.selected_pages[0].deep_snapshot
    deep.final_url = "https://www.example.test/contact/"
    value.source.binding.payload_checksum = request_digest(value.source.payload)
    result = build_site_business_facts(value)
    assert all(str(i.source_locator.url) == "https://www.example.test/contact/" for i in result.evidence_index)
    assert all(str(c.requested_url) == "https://example.test/" for c in result.candidates)


@pytest.mark.parametrize("mutation", [
    lambda c: setattr(c, "scalar_value", "Forged 555"),
    lambda c: setattr(c, "field", "phone"),
    lambda c: setattr(c, "snapshot_id", UUID(int=777)),
    lambda c: setattr(c, "final_url", "https://example.test/other"),
    lambda c: setattr(c, "collected_at", c.collected_at.replace(year=2025)),
    lambda c: setattr(c.origins[0], "start", 0),
    lambda c: setattr(c.origins[0], "end", 999999),
    lambda c: setattr(c.origins[0], "excerpt", "Forged"),
    lambda c: setattr(c.origins[0], "decoded_html_checksum", "sha256:" + "0" * 64),
    lambda c: setattr(c.origins[0], "origin_path", "/payload/selected_pages/1/deep_snapshot/html"),
    lambda c: setattr(c.origins[0], "json_pointer", "/telephone/0"),
    lambda c: setattr(c.origins[0], "record_pointer", "/other"),
])
def test_candidate_tampering_cannot_be_returned(monkeypatch, mutation):
    original = evidence.make_candidate
    def forged(*args):
        candidate = original(*args)
        if candidate.field == "business_name":
            mutation(candidate)
        return candidate
    monkeypatch.setattr(evidence, "make_candidate", forged)
    with pytest.raises(SiteBusinessError) as exc:
        build_site_business_facts(two_pages())
    assert exc.value.error_code == "V22_SITE_FACTS_REFERENCE_INVALID"


@pytest.mark.parametrize("target,key,value", [
    ("item", "original_value", "forged"), ("item", "normalized_value", "forged"),
    ("item", "snapshot_id", UUID(int=777)), ("item", "confidence", "high"),
    ("trace", "origin_paths", ["/wrong"]), ("trace", "snapshot_id", UUID(int=777)),
])
def test_independent_evidence_audit_rejects_changed_output(monkeypatch, target, key, value):
    original = evidence.make_evidence
    def forged(candidate):
        for item, trace in original(candidate):
            setattr(item if target == "item" else trace, key, value)
            yield item, trace
    monkeypatch.setattr(evidence, "make_evidence", forged)
    with pytest.raises(SiteBusinessError, match="REFERENCE_INVALID"):
        build_site_business_facts(request())


def test_candidate_hash_collision_fails_instead_of_selecting_a_value(monkeypatch):
    monkeypatch.setattr(evidence, "candidate_id", lambda c: "sf_" + "0" * 64)
    with pytest.raises(SiteBusinessError, match="ID_CONFLICT"):
        build_site_business_facts(request())


def test_evidence_hash_collision_fails_instead_of_overwriting(monkeypatch):
    monkeypatch.setattr(evidence, "evidence_id", lambda c: "ev_" + "0" * 64)
    with pytest.raises(SiteBusinessError, match="ID_CONFLICT"):
        build_site_business_facts(request())


def test_fixed_disclaimers_survive_limitations_cap():
    value = request()
    value.source.payload.limitations = [f"Note {i:03}" for i in range(30)]
    value.source.binding.payload_checksum = request_digest(value.source.payload)
    result = build_site_business_facts(value)
    assert len(result.limitations) == 32
    assert all(len(c.limitations) == 32 for c in result.candidates)
    assert all(len(i.limitations) == 20 and set(evidence.FIXED_NOTES) <= set(i.limitations) for i in result.evidence_index)


def test_result_keeps_candidate_specific_limitations():
    result = build_site_business_facts(request(business_html(url="https://other.test/")))
    assert all(set(c.limitations) <= set(result.limitations) for c in result.candidates)


def test_identity_is_stable_in_fresh_processes():
    code = 'import sys; sys.path.insert(0,"tests"); from site_business_helpers import request; from app.report_v22.site_business_facts import build_site_business_facts; print(build_site_business_facts(request()).model_dump_json())'
    first = subprocess.check_output([sys.executable, "-c", code])
    second = subprocess.check_output([sys.executable, "-c", code])
    assert first == second
