from datetime import timedelta
from uuid import UUID

import pytest

from app.jobs_v22.digest import canonical_json_bytes, request_digest
from app.report_v22.evidence import build_evidence_index, resolve_origin
from app.report_v22.evidence_errors import EvidenceError
from app.report_v22.evidence_models import EvidenceBuildInput, MissingEvidenceSource
from evidence_helpers import NOW, context, first_party_source, site_source
from public_gbp_helpers import evidence_input, public_source, sample_input


def test_public_fields_have_real_origins_and_no_authorized_data_claim():
    request = evidence_input()
    result = build_evidence_index(request)
    assert result.source_summaries[0].gbp_origin == "public_profile"
    assert result.source_summaries[0].business_eligible
    assert not result.coverage_gaps
    assert len(result.evidence_index) == 10
    items = {item.evidence_id: item for item in result.evidence_index}
    for trace in result.source_traces:
        item = items[trace.evidence_id]
        assert item.source_type == "gbp" and item.confidence == "medium"
        assert item.source_locator.external_resource_id == "place_id:fixture-customer-place"
        assert trace.selector.record_context[0] == "customer_public_gbp_v1"
        assert any("not authorized GBP Performance" in note for note in item.limitations)
        for path in trace.origin_paths:
            assert resolve_origin(request.sources[0].model_dump(mode="json"), path) == item.original_value
    assert any(item.original_value is False for item in result.evidence_index)


def test_absent_url_not_replaced_with_confirmation_and_keys_need_matching():
    raw = sample_input()
    raw["record"]["observed_public_gbp_url"] = None
    raw["record"]["observed_entity_keys"][0]["kind"] = "data_id"
    result = build_evidence_index(evidence_input(raw))
    assert all(item.source_locator.url is None for item in result.evidence_index)
    assert all(item.source_locator.external_resource_id == "cid:12345" for item in result.evidence_index)


@pytest.mark.parametrize("state,reason", [("not_returned", "partial"), ("unsupported", "partial"), ("returned_empty", "empty")])
def test_field_unknown_is_coverage_not_business_fact(state, reason):
    raw = sample_input()
    raw["record"]["fields"]["phone"] = dict(state=state, value=None)
    result = build_evidence_index(evidence_input(raw))
    gap, = result.coverage_gaps
    item = next(item for item in result.evidence_index if item.evidence_id == gap.evidence_id)
    trace = next(t for t in result.source_traces if t.evidence_id == gap.evidence_id)
    assert (item.source_type, item.original_value, item.normalized_value, item.confidence) == ("coverage", state, reason, "low")
    assert trace.origin_paths == ["/payload/record/fields/phone/state"]
    assert gap.gbp_origin == "public_profile"


@pytest.mark.parametrize("condition,reason", [("expired", "expired"), ("conflict", "identity_mismatch"), ("unknown", "identity_unconfirmed"), ("failed", "unavailable")])
def test_ineligible_emits_only_source_coverage(condition, reason):
    raw = sample_input()
    if condition == "expired":
        raw["expires_at"] = NOW
    elif condition == "conflict":
        raw["record"]["observed_entity_keys"][0]["value"] = "another-place"
    elif condition == "unknown":
        raw["record"]["observed_entity_keys"] = []
    else:
        raw.update(collection_status="failed", failure_code="timeout", response_checksum=None)
        raw["record"].update(observed_entity_keys=[], observed_public_gbp_url=None)
        for field, wrapper in raw["record"]["fields"].items():
            wrapper.update(state="request_failed", value=[] if field == "service_areas" else None)
    result = build_evidence_index(evidence_input(raw))
    item, = result.evidence_index
    assert item.source_type == "coverage" and item.normalized_value == reason
    assert not result.source_summaries[0].business_eligible
    assert result.source_traces[0].selector.record_context[0] == "customer_public_gbp_v1"


@pytest.mark.parametrize("tamper", ["missing_reference", "case", "site", "confirmed_at", "digest", "target", "binding_identity", "binding_health", "binding_expiry", "binding_time", "checksum", "forged_identity", "two_snapshots", "verified", "oversize"])
def test_binding_failures_are_safe(tamper):
    request = evidence_input()
    source = request.sources[0]
    if tamper == "missing_reference":
        request.context.customer_public_gbp = None
    elif tamper == "case":
        request.context.customer_public_gbp.case_id = UUID(int=901)
    elif tamper == "site":
        request.context.customer_public_gbp.site_url = "https://other.test/"
    elif tamper == "confirmed_at":
        request.context.customer_public_gbp.confirmed_at = NOW + timedelta(days=1)
    elif tamper == "digest":
        source.payload.subject_reference_checksum = "sha256:" + "c" * 64
    elif tamper == "target":
        source.payload.request_target.entity_keys[0].value = "SECRET-other-place"
    elif tamper == "binding_identity":
        source.binding.identity_match_status = "mismatch"
    elif tamper == "binding_health":
        source.binding.health_status = "unavailable"
    elif tamper == "binding_expiry":
        source.binding.expires_at += timedelta(seconds=1)
    elif tamper == "binding_time":
        source.binding.fetched_at += timedelta(seconds=1)
    elif tamper == "forged_identity":
        source.payload.record.observed_entity_keys[0].value = "SECRET-conflicting-place"
    elif tamper == "two_snapshots":
        request.sources.append(public_source(number=72))
    elif tamper == "verified":
        request.context.report_type = "verified_execution"
    elif tamper == "oversize":
        source.payload.limitations = ["SECRET" * 200_000]
    if tamper != "checksum":
        source.binding.payload_checksum = request_digest(source.payload)
    else:
        source.binding.payload_checksum = "sha256:" + "0" * 64
    with pytest.raises(EvidenceError) as exc:
        build_evidence_index(request)
    assert exc.value.error_code.startswith("V22_EVIDENCE_")
    assert "SECRET" not in str(exc.value)


def test_missing_origins_preserve_legacy_first_party_meaning():
    request = evidence_input()
    request.missing_sources = [MissingEvidenceSource(source_type="gbp", reason="not_connected")]
    result = build_evidence_index(request)
    assert any(g.reason == "not_connected" and g.snapshot_id is None for g in result.coverage_gaps)
    request.missing_sources = [MissingEvidenceSource(source_type="gbp", gbp_origin="public_profile")]
    with pytest.raises(EvidenceError):
        build_evidence_index(request)
    legacy = EvidenceBuildInput(context=context(report_type="verified_execution"), sources=[first_party_source("gbp")],
                                missing_sources=[MissingEvidenceSource(source_type="gbp", reason="not_connected")])
    with pytest.raises(EvidenceError):
        build_evidence_index(legacy)


@pytest.mark.parametrize("missing", [dict(source_type="site", gbp_origin="public_profile"), dict(source_type="gbp", gbp_origin="public_profile", reason="not_connected")])
def test_invalid_missing_origin_rejected_at_boundary(missing):
    raw = dict(context=context().model_dump(mode="python"), missing_sources=[missing])
    with pytest.raises(EvidenceError):
        build_evidence_index(raw)


def test_missing_public_needs_no_fake_snapshot_or_reference():
    request = EvidenceBuildInput(context=context(), missing_sources=[MissingEvidenceSource(source_type="gbp", gbp_origin="public_profile")])
    result = build_evidence_index(request)
    assert result.evidence_index == [] and result.source_traces == []
    assert result.coverage_gaps[0].snapshot_id is None
    request.context.report_type = "verified_execution"
    with pytest.raises(EvidenceError):
        build_evidence_index(request)


def test_duplicates_order_and_old_evidence_are_stable():
    request = evidence_input(sample_input(), site_source())
    baseline = build_evidence_index(EvidenceBuildInput(context=context(), sources=[site_source()]))
    result = build_evidence_index(request)
    assert [item for item in result.evidence_index if item.snapshot_id == site_source().binding.snapshot_id] == baseline.evidence_index
    request.sources = [*reversed(request.sources), request.sources[0]]
    assert canonical_json_bytes(build_evidence_index(request)) == canonical_json_bytes(result)
    raw = sample_input()
    raw["record"]["fields"]["service_areas"]["value"] = ["Austin", "Austin"]
    result = build_evidence_index(evidence_input(raw))
    trace, = [t for t in result.source_traces if t.selector.field == "service_areas"]
    assert len(trace.origin_paths) == 2
