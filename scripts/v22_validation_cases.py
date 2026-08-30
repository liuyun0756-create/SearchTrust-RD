"""Strict, test-only reader for the shared v2.2 validation corpus."""

from __future__ import annotations

import copy
import re
from typing import Any


def _require(condition: bool) -> None:
    if not condition:
        raise ValueError("Invalid v2.2 validation case")


def _member(container: Any, key: Any, *, adding: bool = False) -> Any:
    if isinstance(container, dict):
        _require(isinstance(key, str) and key not in {"__proto__", "prototype", "constructor"})
        _require(adding or key in container)
        return container.get(key)
    _require(isinstance(container, list) and type(key) is int and 0 <= key < len(container))
    return container[key]


def apply_operations(base: dict, operations: list) -> dict:
    _require(isinstance(operations, list))
    result = copy.deepcopy(base)
    for operation in operations:
        _require(isinstance(operation, dict))
        kind = operation.get("op")
        expected = {"op", "path", "value"} if kind == "set" else {"op", "path", "keys"} if kind == "reorder_keys" else {"op", "path"}
        _require(isinstance(kind, str) and kind in {"set", "remove", "reorder_keys"} and set(operation) == expected)
        path = operation["path"]
        _require(isinstance(path, list) and len(path) > 0)
        parent = result
        for part in path[:-1]:
            parent = _member(parent, part)
        key = path[-1]
        target = _member(parent, key, adding=kind == "set")
        if kind == "set":
            parent[key] = copy.deepcopy(operation["value"])
        elif kind == "remove":
            del parent[key]
        else:
            keys = operation["keys"]
            _require(isinstance(target, dict) and isinstance(keys, list))
            _require(all(isinstance(k, str) for k in keys))
            _require(len(keys) == len(target) and set(keys) == set(target))
            parent[key] = {k: target[k] for k in keys}
    return result


def load_cases(document: Any, fixtures: dict[str, dict]) -> list[dict]:
    _require(isinstance(document, dict) and set(document) == {"version", "cases"})
    _require(type(document["version"]) is int and document["version"] == 1)
    cases = document["cases"]
    _require(isinstance(cases, list) and len(cases) > 0)
    seen: set[str] = set()
    for case in cases:
        _require(isinstance(case, dict))
        _require(type(case.get("accepted")) is bool)
        fields = {"id", "fixture", "operations", "accepted"}
        if not case["accepted"]:
            fields.add("error")
        _require(set(case) == fields)
        identifier = case["id"]
        _require(isinstance(identifier, str) and re.fullmatch(r"[a-z][a-z0-9_]*", identifier) is not None)
        _require(identifier not in seen)
        seen.add(identifier)
        _require(isinstance(case["fixture"], str) and case["fixture"] in fixtures)
        if not case["accepted"]:
            error = case["error"]
            _require(isinstance(error, dict) and set(error) == {"code", "path"})
            _require(isinstance(error["code"], str) and error["code"] in {"REPORT_CONTRACT_INVALID", "REPORT_REFERENCE_INVALID"})
            _require(isinstance(error["path"], str) and error["path"].startswith("/"))
        apply_operations(fixtures[case["fixture"]], case["operations"])
    return cases
