from copy import deepcopy

import pytest
from pydantic import ValidationError

from app.report_v22.version_diff_models import VersionDiffAuditEntry, VersionDiffBuildResult
from version_diff_helpers import version_diff_request


def test_contracts_are_strict_and_replaced_is_unreachable() -> None:
    raw = version_diff_request().model_dump(mode="json")
    raw["unexpected"] = True
    with pytest.raises(ValidationError):
        type(version_diff_request()).model_validate(raw)

    result = deepcopy(version_diff_request())
    assert result.limits.max_bytes == 20_000_000


def test_audit_requires_previous_finding_only_for_non_new() -> None:
    request = version_diff_request()
    ref = request.verified_reprioritization_result.core_problem_finding
    evidence_id = request.parent_report.evidence_index[0].evidence_id
    with pytest.raises(ValidationError):
        VersionDiffAuditEntry(
            audit_id="vda_example",
            change_type="confirmed",
            previous_finding_id=None,
            current_finding_refs=[ref],
            reason_code="TEST",
            evidence_ids=[evidence_id],
            evidence_basis="direct_relation",
        )


def test_result_schema_rejects_replaced() -> None:
    assert "replaced" in VersionDiffBuildResult.model_json_schema()["$defs"]["VersionDiffEntry"]["properties"]["change_type"]["enum"]
