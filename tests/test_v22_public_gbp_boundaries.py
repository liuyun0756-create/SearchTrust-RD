from copy import deepcopy
from datetime import timedelta
import os
import subprocess
import sys
import warnings

import pytest

from app.jobs_v22.digest import canonical_json_bytes, request_digest
from app.report_v22.evidence import build_evidence_index
from app.report_v22.evidence_errors import EvidenceError
from app.report_v22.evidence_models import EvidenceBuildInput
from app.report_v22.public_gbp_errors import PublicGbpError
from app.report_v22.public_gbp_models import CustomerPublicGbpSnapshotInput
from app.report_v22.public_gbp_snapshot import build_customer_public_gbp_snapshot
from evidence_helpers import NOW, context, site_source, first_party_source
from public_gbp_helpers import evidence_input, sample_input


@pytest.mark.parametrize("mutation", [
    lambda v: v.update(identity_match_status="matched"),
    lambda v: v.update(snapshot_id="00000000-0000-0000-0000-000000000001"),
    lambda v: v.update(health_status="healthy"),
    lambda v: v.update(response_checksum="secret"),
    lambda v: v.update(provider="google_business_profile"),
    lambda v: v.update(request_record_id="req_secret"),
    lambda v: v.update(completed_at=v["completed_at"].replace(tzinfo=None)),
    lambda v: v["reference"].update(confirmed_at=v["started_at"] + timedelta(seconds=1)),
    lambda v: v["reference"].update(confirmation_source="provider"),
    lambda v: v["record"]["fields"]["business_name"].pop("value"),
    lambda v: v["record"]["fields"].pop("phone"),
    lambda v: v["record"]["fields"]["service_area_business"].update(value=0),
    lambda v: v["record"]["fields"]["service_area_business"].update(value=1),
    lambda v: v["record"]["fields"]["service_area_business"].update(value=None),
    lambda v: v["record"]["fields"]["business_name"].update(value="a" * 241),
    lambda v: v["record"]["fields"]["address"].update(value="a" * 501),
    lambda v: v["record"]["fields"]["website_url"].update(value="https://example.test/" + "a" * 2083),
    lambda v: v["record"]["fields"]["service_areas"].update(value=["A"] * 51),
    lambda v: v["record"]["fields"]["service_areas"].update(value=[" "]),
    lambda v: v["record"]["fields"]["service_areas"].update(value=["A" * 241]),
    lambda v: v["record"]["observed_entity_keys"][0].update(value=" key "),
    lambda v: v["record"]["observed_entity_keys"][0].update(value="a" * 481),
    lambda v: v["record"].update(observed_public_gbp_url="https://attacker.test/"),
    lambda v: v.update(limitations=[float("nan")]),
])
def test_strict_nested_boundaries(mutation):
    raw = sample_input()
    mutation(raw)
    with pytest.raises(PublicGbpError, match="V22_PUBLIC_GBP_INPUT_INVALID"):
        build_customer_public_gbp_snapshot(raw)


@pytest.mark.parametrize("website,status", [("https://WWW.EXAMPLE.TEST/path?q=1", "matched"), ("https://sub.example.test/", "mismatch"), ("https://www.www.example.test/", "mismatch")])
def test_only_exact_normalized_host_matches(website, status):
    raw = sample_input()
    raw["record"]["fields"]["website_url"]["value"] = website
    assert build_customer_public_gbp_snapshot(raw).identity_match_status == status


def test_opaque_key_types_case_and_returned_url_do_not_infer_identity():
    raw = sample_input()
    raw["record"]["observed_entity_keys"] = [{"kind": "data_id", "value": "12345"}]
    assert build_customer_public_gbp_snapshot(raw).identity_reasons == ["no_comparable_strong_id"]
    raw["record"]["observed_entity_keys"] = [{"kind": "place_id", "value": "FIXTURE-CUSTOMER-PLACE"}]
    assert build_customer_public_gbp_snapshot(raw).identity_reasons == ["strong_id_conflict"]
    raw = sample_input()
    raw["record"]["observed_public_gbp_url"] = "https://www.google.com/maps?cid=other"
    assert build_customer_public_gbp_snapshot(raw).identity_match_status == "matched"


def test_reordering_entity_keys_is_canonical_and_does_not_mutate_inputs():
    raw = sample_input()
    before = deepcopy(raw)
    baseline = build_customer_public_gbp_snapshot(raw)
    reordered = deepcopy(raw)
    reordered["request_target"]["entity_keys"].reverse()
    reordered["record"]["observed_entity_keys"].reverse()
    assert canonical_json_bytes(build_customer_public_gbp_snapshot(reordered)) == canonical_json_bytes(baseline)
    assert raw == before


def test_observed_contact_text_and_service_areas_preserve_whitespace():
    raw = sample_input()
    raw["record"]["fields"]["business_name"]["value"] = "  Example Plumbing  "
    raw["record"]["fields"]["service_areas"]["value"] = [" Austin ", "Austin"]
    request = evidence_input(raw)
    assert request.sources[0].payload.record.fields.business_name.value == "  Example Plumbing  "
    result = build_evidence_index(request)
    items = {item.evidence_id: item for item in result.evidence_index}
    for trace in result.source_traces:
        if trace.selector.field == "business_name":
            assert items[trace.evidence_id].original_value == items[trace.evidence_id].normalized_value == "  Example Plumbing  "
    areas = [items[t.evidence_id].normalized_value for t in result.source_traces if t.selector.field == "service_areas"]
    assert sorted(areas) == [" Austin ", "Austin"]


def test_exact_size_boundary_checks_input_and_output():
    raw = sample_input()
    model = CustomerPublicGbpSnapshotInput.model_validate(raw)
    result = build_customer_public_gbp_snapshot(model)
    limit = max(len(canonical_json_bytes(model)), len(canonical_json_bytes(result)))
    # Encoding limits itself changes the input size. Converge on the exact bound.
    while True:
        model.limits.max_bytes = limit
        size = max(len(canonical_json_bytes(model)), len(canonical_json_bytes(result)))
        if size == limit:
            break
        limit = size
    assert build_customer_public_gbp_snapshot(model) == result
    model.limits.max_bytes = limit - 1
    with pytest.raises(PublicGbpError, match="LIMIT_EXCEEDED"):
        build_customer_public_gbp_snapshot(model)
    raw["limitations"] = ["a" * 1_000_000]
    with pytest.raises(PublicGbpError, match="LIMIT_EXCEEDED"):
        build_customer_public_gbp_snapshot(raw)


@pytest.mark.parametrize("field,value", [("identity_reasons", ["strong_id_matched", "strong_id_matched"]), ("identity_rule_version", "forged"), ("schema_version", "forged"), ("expires_at", NOW + timedelta(days=31))])
def test_loaded_snapshot_labels_schema_and_ttl_cannot_bypass_builder(field, value):
    request = evidence_input()
    setattr(request.sources[0].payload, field, value)
    request.sources[0].binding.payload_checksum = request_digest(request.sources[0].payload)
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        with pytest.raises(EvidenceError):
            build_evidence_index(request)
    assert not captured


def test_reference_without_snapshot_is_still_validated():
    request = evidence_input()
    request.sources = []
    request.context.customer_public_gbp.confirmed_at = NOW + timedelta(seconds=1)
    with pytest.raises(EvidenceError):
        build_evidence_index(request)


def test_full_time_window_and_expiration_priority():
    raw = sample_input()
    raw["expires_at"] = raw["completed_at"] + timedelta(days=30)
    assert build_customer_public_gbp_snapshot(raw)
    request = evidence_input(raw)
    request.context.evaluated_at = raw["completed_at"] - timedelta(seconds=1)
    with pytest.raises(EvidenceError):
        build_evidence_index(request)
    raw["expires_at"] = NOW
    raw["record"]["observed_entity_keys"][0]["value"] = "mismatch"
    assert build_evidence_index(evidence_input(raw)).coverage_gaps[0].reason == "expired"


def test_global_snapshot_collision_is_rejected_before_adapters(monkeypatch):
    from app.report_v22.evidence_adapters import public_gbp
    request = evidence_input(sample_input(), site_source())
    request.sources[0].binding.snapshot_id = request.sources[1].binding.snapshot_id
    def forbidden(*args):
        pytest.fail("adapter ran before full validation")
    monkeypatch.setattr(public_gbp, "observations", forbidden)
    with pytest.raises(EvidenceError):
        build_evidence_index(request)


def test_long_limitations_never_hide_public_profile_disclaimer():
    raw = sample_input()
    raw["limitations"] = [f"A limitation {i}" for i in range(30)]
    result = build_evidence_index(evidence_input(raw))
    assert len(result.source_summaries[0].limitations) == 32
    for item in result.evidence_index:
        assert len(item.limitations) <= 20
        assert any("not authorized GBP Performance" in note for note in item.limitations)
        assert any("Additional limitations" in note for note in item.limitations)


def test_loaded_payload_byte_limit_includes_derived_fields():
    request = evidence_input()
    payload = request.sources[0].payload
    payload.identity_reasons = ["strong_id_matched"] * 60_000
    request.sources[0].binding.payload_checksum = request_digest(payload)
    with pytest.raises(EvidenceError, match="LIMIT_EXCEEDED"):
        build_evidence_index(request)


def test_old_authorized_gbp_has_explicit_first_party_summary():
    request = EvidenceBuildInput(context=context(report_type="verified_execution"), sources=[first_party_source("gbp")])
    result = build_evidence_index(request)
    assert result.source_summaries[0].gbp_origin == "first_party"


def test_no_network_or_uuid_allocation(monkeypatch):
    import socket
    import uuid
    def forbidden(*args, **kwargs):
        pytest.fail("offline builder attempted IO")
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(uuid, "uuid4", forbidden)
    assert build_evidence_index(evidence_input()).evidence_index


def test_process_hash_seed_does_not_change_fixture_bytes():
    code = "from scripts.export_v22_public_gbp_fixtures import build_resources,ROOT; from hashlib import sha256; print(sha256(b''.join(build_resources(ROOT).values())).hexdigest())"
    values = [subprocess.check_output([sys.executable, "-c", code], env={**os.environ, "PYTHONHASHSEED": seed}) for seed in ("1", "983")]
    assert values[0] == values[1]
