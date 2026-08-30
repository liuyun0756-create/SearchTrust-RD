from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.report_v22.models import ReportV22
from scripts.v22_validation_cases import apply_operations, load_cases

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = {name: json.loads((ROOT / "contracts/v2.2/fixtures" / f"{name}.json").read_text()) for name in ("prospect", "verified")}
DOCUMENT = json.loads((ROOT / "tests/fixtures/report_v22_validation/cases.json").read_text())
CASES = load_cases(DOCUMENT, FIXTURES)


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
def test_shared_validation_case(case: dict) -> None:
    original = copy.deepcopy(FIXTURES[case["fixture"]])
    report = apply_operations(original, case["operations"])
    before = copy.deepcopy(report)
    if case["accepted"]:
        ReportV22.model_validate_json(json.dumps(report))
    else:
        with pytest.raises(ValidationError):
            ReportV22.model_validate_json(json.dumps(report))
    assert report == before
    assert original == FIXTURES[case["fixture"]]


@pytest.mark.parametrize("operation", [
    {"op": "eval", "path": ["x"]},
    {"op": [], "path": ["x"]},
    {"op": "remove", "path": ["missing"]},
    {"op": "set", "path": ["missing", "x"], "value": 1},
    {"op": "set", "path": [], "value": 1},
    {"op": "set", "path": ["items", -1], "value": 1},
    {"op": "set", "path": ["items", 1], "value": 1},
    {"op": "set", "path": ["items", "0"], "value": 1},
    {"op": "set", "path": ["items", True], "value": 1},
    {"op": "set", "path": ["__proto__"], "value": 1},
    {"op": "set", "path": ["x"], "value": 1, "extra": True},
    {"op": "reorder_keys", "path": ["object"], "keys": ["a", "a"]},
    {"op": "reorder_keys", "path": ["object"], "keys": ["a"]},
    {"op": "reorder_keys", "path": ["items"], "keys": ["0"]},
])
def test_bad_operation_is_not_silently_skipped(operation: dict) -> None:
    base = {"items": [1], "object": {"a": 1, "b": 2}}
    with pytest.raises(ValueError):
        apply_operations(base, [operation])


@pytest.mark.parametrize("kind", ["duplicate", "unknown_fixture", "empty", "bad_result", "bad_error", "unknown_field"])
def test_bad_corpus_is_rejected(kind: str) -> None:
    document = copy.deepcopy(DOCUMENT)
    if kind == "duplicate":
        document["cases"].append(document["cases"][0])
    elif kind == "unknown_fixture":
        document["cases"][0]["fixture"] = "missing"
    elif kind == "empty":
        document["cases"] = []
    elif kind == "bad_result":
        document["cases"][0]["accepted"] = "true"
    elif kind == "bad_error":
        document["cases"][2]["error"]["code"] = "unknown"
    else:
        document["cases"][0]["extra"] = True
    with pytest.raises(ValueError):
        load_cases(document, FIXTURES)


def test_operations_clone_values_and_preserve_requested_key_order() -> None:
    value = {"nested": []}
    result = apply_operations({"items": [1, 2], "object": {"a": 1, "b": 2}}, [
        {"op": "set", "path": ["new"], "value": value},
        {"op": "remove", "path": ["items", 0]},
        {"op": "reorder_keys", "path": ["object"], "keys": ["b", "a"]},
    ])
    result["new"]["nested"].append(True)
    assert value == {"nested": []}
    assert result["items"] == [2]
    assert list(result["object"]) == ["b", "a"]
