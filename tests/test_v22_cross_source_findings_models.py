from datetime import timedelta

import pytest
from pydantic import ValidationError

from app.jobs_v22.digest import request_digest
from app.report_v22.cross_source_findings_models import (
    CrossSourceFindingsInput,
    CrossSourcePairAssessment,
)
from app.report_v22.first_party_findings import build_first_party_findings
from test_v22_first_party_findings import CASE_ID, NOW, PARENT_ID, ga4_value, gsc_value, request, trusted


def cross_input():
    source = request(trusted("gsc", gsc_value(), 801), trusted("ga4", ga4_value(), 802))
    result = build_first_party_findings(source)
    return CrossSourceFindingsInput(
        case_id=CASE_ID, parent_report_id=PARENT_ID, evaluated_at=NOW + timedelta(hours=1),
        normalized_domain="example.test", first_party_input=source, first_party_result=result,
        first_party_input_checksum=request_digest(source), first_party_result_checksum=request_digest(result),
    )


def test_input_binds_exact_first_party_input_and_result() -> None:
    value = cross_input()
    with pytest.raises(ValidationError):
        CrossSourceFindingsInput.model_validate(value.model_dump(mode="python") | {
            "first_party_result_checksum": f"sha256:{'0' * 64}"
        })
    with pytest.raises(ValidationError):
        CrossSourceFindingsInput.model_validate(value.model_dump(mode="python") | {
            "normalized_domain": "Example.test"
        })


def test_pair_assessment_requires_canonical_sources() -> None:
    with pytest.raises(ValidationError):
        CrossSourcePairAssessment(pair="gsc_ga4", source_types=("ga4", "gsc"),
            snapshot_ids=(None, None), state="not_checked")


def test_input_rejects_nonfinite_nested_values() -> None:
    value = cross_input().model_dump(mode="python")
    value["first_party_result"]["rule_evaluations"][0]["impact_score"] = float("nan")
    with pytest.raises(ValidationError):
        CrossSourceFindingsInput.model_validate(value)
