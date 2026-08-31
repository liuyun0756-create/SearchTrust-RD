from copy import deepcopy
from datetime import timedelta
import pytest

from app.jobs_v22.digest import canonical_json_bytes
from app.report_v22.public_gbp_errors import PublicGbpError
from app.report_v22.public_gbp_snapshot import build_customer_public_gbp_snapshot
from public_gbp_helpers import sample_input


def test_snapshot_is_pure_preserves_false_and_explicit_fields():
    value = sample_input()
    before = deepcopy(value)
    result = build_customer_public_gbp_snapshot(value)
    assert value == before
    assert result.identity_match_status == "matched"
    assert result.identity_reasons == ["strong_id_matched"]
    assert result.health_status == "healthy"
    assert result.record.fields.service_area_business.value is False
    assert "snapshot_id" not in result.model_dump()
    assert canonical_json_bytes(result) == canonical_json_bytes(build_customer_public_gbp_snapshot(value))


@pytest.mark.parametrize("case,expected,reason", [
    ("conflict", "mismatch", "strong_id_conflict"),
    ("no_keys", "needs_confirmation", "no_comparable_strong_id"),
    ("wrong_domain", "mismatch", "website_domain_conflict"),
    ("no_website", "matched", "website_not_observed"),
    ("different_contact", "matched", "strong_id_matched"),
])
def test_identity_uses_strong_keys_not_contact_text(case, expected, reason):
    value = sample_input()
    if case == "conflict":
        value["record"]["observed_entity_keys"][1]["value"] = "999"
    elif case == "no_keys":
        value["record"]["observed_entity_keys"] = []
    elif case == "wrong_domain":
        value["record"]["fields"]["website_url"]["value"] = "https://competitor-1.test/"
    elif case == "no_website":
        value["record"]["fields"]["website_url"] = {"state": "not_returned", "value": None}
    else:
        for field in ("business_name", "address", "phone"):
            value["record"]["fields"][field]["value"] = "Different contact text"
    result = build_customer_public_gbp_snapshot(value)
    assert result.identity_match_status == expected
    assert reason in result.identity_reasons


@pytest.mark.parametrize("mutation", [
    lambda v: v.update(unexpected="secret"),
    lambda v: v["record"]["fields"]["service_area_business"].update(value="false"),
    lambda v: v["record"]["fields"]["business_name"].update(state="not_returned"),
    lambda v: v["record"]["fields"]["phone"].pop("state"),
    lambda v: v["record"]["fields"]["service_areas"].update(value=[]),
    lambda v: v["record"]["fields"]["business_name"].update(value=" " * 4),
    lambda v: v["record"]["fields"]["phone"].update(value="x" * 121),
    lambda v: v["record"]["observed_entity_keys"].append({"kind": "cid", "value": "789"}),
    lambda v: v.update(expires_at=v["completed_at"] + timedelta(days=30, seconds=1)),
    lambda v: v.update(expires_at=v["completed_at"]),
    lambda v: v.update(started_at=v["completed_at"] + timedelta(seconds=1)),
    lambda v: v.update(response_checksum=None),
    lambda v: v["record"]["fields"]["phone"].update(state="request_failed", value=None),
    lambda v: v["reference"].update(public_gbp_url="https://attacker.test/?secret=hidden"),
])
def test_invalid_input_is_rejected_with_payload_free_error(mutation):
    value = sample_input()
    mutation(value)
    with pytest.raises(PublicGbpError) as exc:
        build_customer_public_gbp_snapshot(value)
    assert exc.value.error_code == "V22_PUBLIC_GBP_INPUT_INVALID"
    assert "secret" not in str(exc.value) + exc.value.user_message


def test_request_target_must_match_independent_reference():
    value = deepcopy(sample_input())
    value["request_target"] = {**value["request_target"], "entity_keys": []}
    with pytest.raises(PublicGbpError, match="V22_PUBLIC_GBP_REFERENCE_INVALID"):
        build_customer_public_gbp_snapshot(value)


def test_failure_requires_all_fields_to_be_unknown():
    value = sample_input()
    value.update(collection_status="failed", failure_code="timeout", response_checksum=None)
    with pytest.raises(PublicGbpError):
        build_customer_public_gbp_snapshot(value)
    value["record"]["observed_entity_keys"] = []
    value["record"]["observed_public_gbp_url"] = None
    for field, row in value["record"]["fields"].items():
        row.update(state="request_failed", value=[] if field == "service_areas" else None)
    result = build_customer_public_gbp_snapshot(value)
    assert result.health_status == "unavailable"
    assert result.identity_reasons == ["lookup_failed"]


def test_size_limit_can_only_be_lowered():
    value = sample_input()
    value["limits"] = {"max_bytes": 1}
    with pytest.raises(PublicGbpError, match="V22_PUBLIC_GBP_LIMIT_EXCEEDED"):
        build_customer_public_gbp_snapshot(value)
    value["limits"] = {"max_bytes": 1_000_001}
    with pytest.raises(PublicGbpError, match="V22_PUBLIC_GBP_INPUT_INVALID"):
        build_customer_public_gbp_snapshot(value)
