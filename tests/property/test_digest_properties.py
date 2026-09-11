from __future__ import annotations

from copy import deepcopy

from hypothesis import given

from app.jobs_v22.digest import canonical_json_bytes, request_digest
from property.strategies import JSON_VALUES


@given(JSON_VALUES)
def test_canonical_digest_is_repeatable_and_does_not_mutate_input(value) -> None:
    before = deepcopy(value)
    first = canonical_json_bytes(value)
    second = canonical_json_bytes(value)

    assert first == second
    assert request_digest(value) == request_digest(value)
    assert value == before


@given(
    JSON_VALUES,
    JSON_VALUES,
    JSON_VALUES,
)
def test_mapping_insertion_order_never_changes_digest(first, second, third) -> None:
    forward = {"first": first, "second": second, "third": third}
    reverse = {key: forward[key] for key in reversed(tuple(forward))}

    assert canonical_json_bytes(forward) == canonical_json_bytes(reverse)
    assert request_digest(forward) == request_digest(reverse)


@given(JSON_VALUES, JSON_VALUES)
def test_nested_mapping_order_is_semantically_irrelevant(left, right) -> None:
    first = {"outer": {"a": left, "b": right}, "stable": True}
    second = {"stable": True, "outer": {"b": right, "a": left}}

    assert request_digest(first) == request_digest(second)
