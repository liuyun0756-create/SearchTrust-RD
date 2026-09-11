"""Deterministic builders and semantic checks for full V2.2 report goldens."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from pydantic import BaseModel

from app.report_v22.execution_plan import build_execution_plan
from app.report_v22.models import ReportV22
from app.report_v22.version_diff import build_version_diff
from app.report_v22.version_diff_models import VersionDiffBuildResult
from execution_plan_helpers import execution_plan_request
from version_diff_helpers import parent_report, version_diff_request


GOLDEN_NAMES = ("prospect", "verified", "version_change")

# All current scenarios use fixed clocks and UUIDs, so no field needs to be
# normalized away. Keeping the allowlist explicit makes future runtime values
# an intentional, reviewed contract change instead of a silent deletion.
NORMALIZATION_ALLOWLIST: Mapping[str, frozenset[str]] = {
    "prospect": frozenset(),
    "verified": frozenset(),
    "version_change": frozenset(),
}


class GoldenReportError(ValueError):
    """Raised when a golden scenario is incomplete or internally inconsistent."""


def _json_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return deepcopy(value)


def _set_path(payload: Any, path: str, replacement: Any) -> None:
    current = payload
    parts = path.split(".")
    for part in parts[:-1]:
        if isinstance(current, list):
            current = current[int(part)]
        elif isinstance(current, dict) and part in current:
            current = current[part]
        else:
            raise GoldenReportError(f"unknown normalization path: {path}")
    leaf = parts[-1]
    if isinstance(current, list):
        index = int(leaf)
        if index >= len(current):
            raise GoldenReportError(f"unknown normalization path: {path}")
        current[index] = replacement
    elif isinstance(current, dict) and leaf in current:
        current[leaf] = replacement
    else:
        raise GoldenReportError(f"unknown normalization path: {path}")


def normalize_payload(
    name: str,
    value: Any,
    *,
    replacements: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply only explicitly approved non-semantic replacements."""

    if name not in NORMALIZATION_ALLOWLIST:
        raise GoldenReportError(f"unknown golden scenario: {name}")
    replacements = replacements or {}
    requested = frozenset(replacements)
    allowed = NORMALIZATION_ALLOWLIST[name]
    if requested != allowed:
        undeclared = sorted(requested - allowed)
        missing = sorted(allowed - requested)
        raise GoldenReportError(
            f"normalization paths differ for {name}; undeclared={undeclared}, missing={missing}"
        )
    payload = _json_value(value)
    if not isinstance(payload, dict):
        raise GoldenReportError(f"golden scenario {name} must be a JSON object")
    for path, replacement in replacements.items():
        _set_path(payload, path, replacement)
    return payload


def build_golden_payloads() -> dict[str, dict[str, Any]]:
    prospect = parent_report()
    verified = build_execution_plan(execution_plan_request()).report
    version_change = build_version_diff(version_diff_request())
    payloads = {
        "prospect": normalize_payload("prospect", prospect),
        "verified": normalize_payload("verified", verified),
        "version_change": normalize_payload("version_change", version_change),
    }
    for name, payload in payloads.items():
        assert_golden_semantics(name, payload)
    return payloads


def canonical_pretty_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            _json_value(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def build_golden_artifacts() -> dict[str, bytes]:
    return {
        f"{name}.json": canonical_pretty_bytes(payload)
        for name, payload in build_golden_payloads().items()
    }


def artifact_hashes(artifacts: Mapping[str, bytes]) -> dict[str, str]:
    return {
        name: f"sha256:{hashlib.sha256(payload).hexdigest()}"
        for name, payload in sorted(artifacts.items())
    }


def _report_semantics(report: ReportV22) -> None:
    evidence_ids = {item.evidence_id for item in report.evidence_index}
    finding_ids = {item.finding_id for item in report.findings}
    action_ids = [item.action_id for item in report.top_actions]
    for finding in report.findings:
        if not set([*finding.evidence_ids, *finding.comparator_ids]) <= evidence_ids:
            raise GoldenReportError(f"finding {finding.finding_id} has dangling evidence")
    for action in report.top_actions:
        if not set(action.finding_ids) <= finding_ids:
            raise GoldenReportError(f"action {action.action_id} has dangling findings")
        earlier = set(action_ids[: action.sequence - 1])
        if not set(action.dependencies) <= earlier:
            raise GoldenReportError(f"action {action.action_id} depends on a later action")
    if report.client_summary.action_ids != action_ids:
        raise GoldenReportError("client summary does not preserve action priority order")


def _version_change_semantics(result: VersionDiffBuildResult) -> None:
    if result.version_diff.kind != "upgrade":
        raise GoldenReportError("version-change fixture must contain an upgrade")
    if not result.version_diff.entries:
        raise GoldenReportError("version-change fixture must contain visible changes")
    if any(
        entry.previous_finding is not None
        and entry.previous_finding.report_id != result.parent_report_id
        for entry in result.version_diff.entries
    ):
        raise GoldenReportError("version changes are not bound to the parent report")
    visible = {
        entry.previous_finding.finding_id
        for entry in result.version_diff.entries
        if entry.previous_finding is not None
    }
    unchanged = {item.finding_id for item in result.unchanged_previous_findings}
    if visible & unchanged:
        raise GoldenReportError("changed and unchanged parent findings overlap")
    if len(result.audit) != len(result.version_diff.entries):
        raise GoldenReportError("visible version changes are missing audit entries")


def assert_golden_semantics(name: str, payload: Mapping[str, Any]) -> None:
    """Validate schemas plus cross-reference and ordering invariants."""

    if name in {"prospect", "verified"}:
        report = ReportV22.model_validate_json(canonical_pretty_bytes(payload))
        expected_type = "prospect" if name == "prospect" else "verified_execution"
        if report.report_version.report_type != expected_type:
            raise GoldenReportError(f"{name} report type is incorrect")
        _report_semantics(report)
        return
    if name == "version_change":
        _version_change_semantics(
            VersionDiffBuildResult.model_validate_json(canonical_pretty_bytes(payload))
        )
        return
    raise GoldenReportError(f"unknown golden scenario: {name}")
