from uuid import UUID

from app.jobs_v22.digest import canonical_json_bytes, request_digest


def test_canonical_digest_is_stable_across_mapping_order() -> None:
    first = {"case_id": UUID("11111111-1111-4111-8111-111111111111"), "limits": {"b": 2, "a": 1}}
    second = {"limits": {"a": 1, "b": 2}, "case_id": UUID("11111111-1111-4111-8111-111111111111")}

    assert canonical_json_bytes(first) == canonical_json_bytes(second)
    assert request_digest(first) == request_digest(second)
    assert request_digest(first).startswith("sha256:")


def test_array_order_remains_significant() -> None:
    assert request_digest({"queries": ["one", "two"]}) != request_digest({"queries": ["two", "one"]})

