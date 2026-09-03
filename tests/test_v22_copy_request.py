from __future__ import annotations

from copy import deepcopy
import os
import subprocess
import sys

import pytest

from app.jobs_v22.digest import canonical_json_bytes, request_digest
from app.report_v22.actions import canonical_public_findings
from app.report_v22.copy_contract import build_copy_request, canonical_public_action_plan
from app.report_v22.copy_errors import PublicCopyInputError
from app.report_v22.copy_models import PublicCopyLimits
from v22_copy_helpers import copy_request, public_findings, public_plan


def test_request_is_stable_and_bound_to_the_complete_action_plan() -> None:
    findings = public_findings()
    plan = public_plan(findings)
    before_findings = canonical_json_bytes(findings)
    before_plan = canonical_json_bytes(plan)
    first = build_copy_request(findings, plan)
    second = build_copy_request(findings, plan)

    assert first == second
    assert first.findings_checksum == request_digest(canonical_public_findings(findings))
    assert first.action_plan_checksum == request_digest(canonical_public_action_plan(plan))
    assert [action.sequence for action in first.actions] == [1, 2, 3]
    assert canonical_json_bytes(findings) == before_findings
    assert canonical_json_bytes(plan) == before_plan


def test_reordered_findings_input_produces_the_same_request() -> None:
    findings = public_findings()
    baseline = copy_request(findings, public_plan(findings))
    reordered = deepcopy(findings)
    reordered.findings.reverse()
    reordered.rule_evaluations.reverse()
    reordered.evidence_result.evidence_index.reverse()
    reordered.evidence_result.source_traces.reverse()
    reordered_plan = public_plan(reordered)

    assert build_copy_request(reordered, reordered_plan) == baseline


def test_request_contains_only_selected_copy_inputs() -> None:
    encoded = canonical_json_bytes(copy_request()).decode()

    assert "implementation_steps" not in encoded
    assert "selection_audit" not in encoded
    assert "unselected_finding_ids" not in encoded
    assert "evidence_index" not in encoded
    assert "validation_metrics" not in encoded
    assert "dependencies" not in encoded
    assert "required_client_assets" not in encoded
    assert "finding_statement" in encoded
    assert "required_limitation" in encoded


def test_facts_have_stable_ids_and_cannot_cross_action_boundaries() -> None:
    request_value = copy_request()
    fact_ids = [fact.fact_id for action in request_value.actions for fact in action.facts]
    assert len(fact_ids) == len(set(fact_ids))
    for action in request_value.actions:
        assert action.required_limitation_ids
        assert all(fact.action_id == action.action_id for fact in action.facts)
        assert all(
            set(fact.source_finding_ids) <= set(action.finding_ids)
            for fact in action.facts
        )


def test_findings_checksum_tampering_fails_without_mutating_inputs() -> None:
    findings = public_findings()
    plan = public_plan(findings)
    before_findings = canonical_json_bytes(findings)
    plan.findings_checksum = "sha256:" + "0" * 64
    before_plan = canonical_json_bytes(plan)

    with pytest.raises(PublicCopyInputError) as exc:
        build_copy_request(findings, plan)

    assert exc.value.error_code == "V22_COPY_CHECKSUM_MISMATCH"
    assert canonical_json_bytes(findings) == before_findings
    assert canonical_json_bytes(plan) == before_plan


def test_copy_requirement_tampering_is_a_nonretryable_catalog_failure() -> None:
    findings = public_findings()
    plan = public_plan(findings)
    plan.actions[0].copy_requirements.required_limitations = ["Invent a result."]

    with pytest.raises(PublicCopyInputError) as exc:
        build_copy_request(findings, plan)

    assert exc.value.error_code == "V22_COPY_CHECKSUM_MISMATCH"
    assert exc.value.retryable is False


def test_non_copy_action_field_tampering_is_rejected_before_dify() -> None:
    findings = public_findings()
    plan = public_plan(findings)
    plan.actions[0].implementation_steps[0].instruction = "Injected instruction"

    with pytest.raises(PublicCopyInputError) as exc:
        build_copy_request(findings, plan)
    assert exc.value.error_code == "V22_COPY_CHECKSUM_MISMATCH"
    assert exc.value.retryable is False


def test_input_and_fact_limits_fail_before_a_request_is_returned() -> None:
    findings = public_findings()
    plan = public_plan(findings)
    with pytest.raises(PublicCopyInputError) as exc:
        build_copy_request(
            findings,
            plan,
            limits=PublicCopyLimits(max_input_bytes=1),
        )
    assert exc.value.error_code == "V22_COPY_LIMIT_EXCEEDED"

    with pytest.raises(PublicCopyInputError) as exc:
        build_copy_request(
            findings,
            plan,
            limits=PublicCopyLimits(max_facts_per_action=1),
        )
    assert exc.value.error_code == "V22_COPY_LIMIT_EXCEEDED"


def test_request_is_stable_across_python_hash_seeds() -> None:
    program = (
        "import sys; sys.path.insert(0, 'tests'); "
        "from v22_copy_helpers import copy_request; "
        "print(copy_request().model_dump_json())"
    )
    outputs = [
        subprocess.check_output(
            [sys.executable, "-c", program],
            env={**os.environ, "PYTHONHASHSEED": str(seed)},
        )
        for seed in (7, 81)
    ]
    assert outputs[0] == outputs[1]
