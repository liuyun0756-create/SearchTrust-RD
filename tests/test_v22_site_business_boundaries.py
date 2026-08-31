import builtins
from datetime import timedelta
import socket
import time
import uuid
import warnings

import pytest

from app.jobs_v22.digest import canonical_json_bytes, request_digest
from app.report_v22.site_business_errors import SiteBusinessError
from app.report_v22.site_business_facts import build_site_business_facts
from app.report_v22.site_business_jsonld import decode_json
from app.report_v22.site_business_models import SiteBusinessLimits
from site_business_helpers import request, two_pages


@pytest.mark.parametrize("key", list(SiteBusinessLimits.model_fields))
def test_each_limit_can_only_be_lowered(key):
    value = request()
    setattr(value.limits, key, getattr(value.limits, key) + 1)
    with pytest.raises(SiteBusinessError, match="INPUT_INVALID"):
        build_site_business_facts(value)


@pytest.mark.parametrize("bad", [True, "2", 1.0, None, -1, 0])
def test_limits_reject_coercion(bad):
    value = request()
    value.limits.max_candidates = bad
    with pytest.raises(SiteBusinessError, match="INPUT_INVALID"):
        build_site_business_facts(value)


@pytest.mark.parametrize("key,threshold", [
    ("max_inventory_pages", 2), ("max_deep_pages", 2),
    ("max_html_nodes_per_page", 2), ("max_html_nodes_total", 4),
    ("max_candidates_per_page", 1), ("max_candidates", 2),
    ("max_origins", 2), ("max_evidence", 2),
])
def test_count_limits_accept_exact_boundary_and_reject_next(key, threshold):
    value = two_pages("<address>A</address>")
    setattr(value.limits, key, threshold)
    build_site_business_facts(value)
    if threshold > 1:
        setattr(value.limits, key, threshold - 1)
    else:
        for selected in value.source.payload.selected_pages:
            selected.deep_snapshot.html += '<a href="tel:555">Call</a>'
        value.source.binding.payload_checksum = request_digest(value.source.payload)
    with pytest.raises(SiteBusinessError, match="LIMIT_EXCEEDED"):
        build_site_business_facts(value)


@pytest.mark.parametrize("key", ["max_html_bytes_per_page", "max_html_bytes_total"])
def test_html_utf8_byte_limits_are_not_character_limits(key):
    value = two_pages("<address>中文</address>")
    threshold = len(value.source.payload.selected_pages[0].deep_snapshot.html.encode()) * (2 if key.endswith("total") else 1)
    setattr(value.limits, key, threshold)
    build_site_business_facts(value)
    setattr(value.limits, key, threshold - 1)
    with pytest.raises(SiteBusinessError, match="LIMIT_EXCEEDED"):
        build_site_business_facts(value)


@pytest.mark.parametrize("key,html,threshold", [
    ("max_html_depth", "<div><address>A</address></div>", 2),
    ("max_json_depth", '<script type="application/ld+json">{"@type":["LocalBusiness"],"name":"A"}</script>', 2),
    ("max_jsonld_blocks", '<script type="application/ld+json">{}</script>' * 2, 2),
    ("max_field_values", '<script type="application/ld+json">{"@type":"LocalBusiness","telephone":["555-1","555-2"]}</script>', 2),
])
def test_structural_limits_fail_whole_operation(key, html, threshold):
    value = request(html)
    setattr(value.limits, key, threshold)
    build_site_business_facts(value)
    setattr(value.limits, key, threshold - 1)
    with pytest.raises(SiteBusinessError, match="LIMIT_EXCEEDED"):
        build_site_business_facts(value)


@pytest.mark.parametrize("key", ["max_jsonld_bytes", "max_json_nodes_per_page"])
def test_json_budget_checks_exact_boundary(key):
    raw = '{"@type":"LocalBusiness","name":"中文"}'
    value = request('<script type="application/ld+json">' + raw + '</script>')
    counter = [0]
    decode_json(raw, value.limits, counter)
    threshold = len(raw.encode()) if key == "max_jsonld_bytes" else counter[0]
    setattr(value.limits, key, threshold)
    build_site_business_facts(value)
    setattr(value.limits, key, threshold - 1)
    with pytest.raises(SiteBusinessError, match="LIMIT_EXCEEDED"):
        build_site_business_facts(value)


def test_json_node_budget_is_shared_between_blocks():
    value = request('<script type="application/ld+json">{}</script>' * 2)
    value.limits.max_json_nodes_per_page = 1
    with pytest.raises(SiteBusinessError, match="LIMIT_EXCEEDED"):
        build_site_business_facts(value)


@pytest.mark.parametrize("key", ["max_input_bytes", "max_output_bytes"])
def test_complete_serialized_size_limits(key):
    value = request()
    if key == "max_input_bytes":
        for _ in range(3):
            value.limits.max_input_bytes = len(canonical_json_bytes(value))
        size = len(canonical_json_bytes(value))
    else:
        size = len(canonical_json_bytes(build_site_business_facts(value)))
    setattr(value.limits, key, size)
    build_site_business_facts(value)
    setattr(value.limits, key, size - 1)
    with pytest.raises(SiteBusinessError, match="LIMIT_EXCEEDED"):
        build_site_business_facts(value)


@pytest.mark.parametrize("mutation", [
    lambda r: setattr(r.context, "evaluated_at", r.context.evaluated_at.replace(tzinfo=None)),
    lambda r: setattr(r.source.binding, "health_status", "SECRET_INVALID"),
    lambda r: setattr(r.source.payload.selected_pages[0], "selection_score", float("nan")),
    lambda r: setattr(r.source.payload.selected_pages[0].deep_snapshot, "html", {"SECRET_INVALID": 1}),
    lambda r: setattr(r.source.payload.selected_pages[0].deep_snapshot, "status_code", "200"),
    lambda r: setattr(r.source.payload.selected_pages[0].deep_snapshot, "response_bytes", True),
])
def test_invalid_nested_models_are_rechecked_without_warning_payloads(mutation):
    value = request()
    mutation(value)
    with warnings.catch_warnings(record=True) as seen, pytest.raises(SiteBusinessError) as exc:
        warnings.simplefilter("always")
        build_site_business_facts(value)
    assert exc.value.error_code == "V22_SITE_FACTS_INPUT_INVALID"
    assert not seen
    assert "SECRET_INVALID" not in str(exc.value) + exc.value.user_message


@pytest.mark.parametrize("field", ["gbp", "competitors", "business_name", "confirmed_name", "primary_phone"])
def test_extra_context_cannot_supply_business_facts(field):
    raw = request().model_dump(mode="python")
    raw["context"][field] = "Untrusted value"
    with pytest.raises(SiteBusinessError, match="INPUT_INVALID"):
        build_site_business_facts(raw)


def test_expiry_wins_over_healthy_and_identity_tags():
    value = request()
    value.source.binding.health_status = "healthy"
    value.source.binding.expires_at = value.context.evaluated_at
    value.source.binding.identity_match_status = "mismatch"
    result = build_site_business_facts(value)
    assert result.source_status.reason == "source_expired" and not result.pages


@pytest.mark.parametrize("field,new", [("page_type", "contact"), ("crawl_depth", 1), ("collected_at", "before")])
def test_selected_deep_context_is_bound(field, new):
    value = request()
    deep = value.source.payload.selected_pages[0].deep_snapshot
    setattr(deep, field, value.source.payload.started_at - timedelta(seconds=1) if new == "before" else new)
    value.source.binding.payload_checksum = request_digest(value.source.payload)
    with pytest.raises(SiteBusinessError, match="BINDING_INVALID"):
        build_site_business_facts(value)


def test_all_inventory_pages_are_retained_including_not_selected_and_failed():
    from pydantic import HttpUrl
    from app.collectors.site_inventory_models import InventoryPageRecord
    value = two_pages()
    p = value.source.payload
    p.selected_pages.pop()
    p.deep_analyzed_count = 1
    p.pages.append(InventoryPageRecord(url=HttpUrl("https://example.test/blocked"), discovery_sources=["seed"],
                                     crawl_depth=1, check_status="robots_disallowed", page_type="other", error_code="unsafe_target"))
    p.discovered_url_count = p.source_counts[0].count = 3
    value.source.binding.payload_checksum = request_digest(p)
    result = build_site_business_facts(value)
    assert len(result.pages) == 3
    blocked = next(page for page in result.pages if str(page.requested_url).endswith("blocked"))
    assert blocked.inventory_check_status == "robots_disallowed" and blocked.inventory_error_code == "unsafe_target"
    assert blocked.diagnostics[0].code == "page_not_checked"
    missing = next(page for page in result.pages if str(page.requested_url).endswith("contact"))
    assert missing.diagnostics[0].code == "deep_snapshot_missing"


def test_builder_uses_no_network_clock_file_access_or_uuid_allocation(monkeypatch):
    value = request()
    def forbidden(*args, **kwargs):
        raise AssertionError("Unexpected IO")
    with monkeypatch.context() as patch:
        for module, name in [(builtins, "open"), (socket, "getaddrinfo"), (socket, "create_connection"),
                             (uuid, "uuid4"), (uuid, "uuid1"), (time, "time")]:
            patch.setattr(module, name, forbidden)
        result = build_site_business_facts(value)
    assert len(result.candidates) == 6


def test_unclosed_script_still_obeys_block_byte_budget():
    value = request('<script type="application/ld+json">' + 'x' * 100)
    value.limits.max_jsonld_bytes = 99
    with pytest.raises(SiteBusinessError, match="LIMIT_EXCEEDED"):
        build_site_business_facts(value)


def test_unsupported_component_array_cannot_bypass_array_budget():
    from site_business_helpers import business_html
    value = request(business_html(telephone=None, areaServed=None, address={"streetAddress": ["A", "B"]}))
    value.limits.max_field_values = 1
    with pytest.raises(SiteBusinessError, match="LIMIT_EXCEEDED"):
        build_site_business_facts(value)


def test_nested_model_inside_dict_cannot_bypass_full_revalidation():
    value = request()
    value.source.binding.health_status = "SECRET_INVALID"
    raw = {"context": value.context, "source": value.source}
    with warnings.catch_warnings(record=True) as seen, pytest.raises(SiteBusinessError, match="INPUT_INVALID"):
        warnings.simplefilter("always")
        build_site_business_facts(raw)
    assert not seen


def test_cyclic_invalid_input_fails_with_safe_error():
    value = {}
    value["source"] = value
    with pytest.raises(SiteBusinessError, match="INPUT_INVALID"):
        build_site_business_facts(value)


def test_source_default_empty_selection_is_valid_and_not_checked():
    value = request()
    value.source.payload.selected_pages = []
    value.source.payload.deep_analyzed_count = 0
    value.source.binding.payload_checksum = request_digest(value.source.payload)
    raw = value.model_dump(mode="python")
    del raw["source"]["payload"]["selected_pages"]
    result = build_site_business_facts(raw)
    assert not result.candidates
    assert result.pages[0].diagnostics[0].code == "deep_snapshot_missing"
