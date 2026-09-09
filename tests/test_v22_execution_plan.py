from copy import deepcopy

import pytest

from app.jobs_v22.digest import canonical_json_bytes, request_digest
from app.report_v22.execution_plan import build_execution_plan
from app.report_v22.execution_plan_errors import ExecutionPlanError
from execution_plan_helpers import execution_plan_request


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_builder_assembles_verified_report_and_one_action_per_phase() -> None:
    request = execution_plan_request()
    parent_before = canonical_json_bytes(request.version_diff_input.parent_report)
    result = build_execution_plan(request)
    assert [item.sequence for item in result.actions] == [1, 2, 3]
    assert [item.action_id for item in result.actions] == [item.action_id for item in result.roadmap]
    assert all(item.primary_metric for item in result.actions)
    assert all(item.metric_rule_id.startswith("V22.EXECUTION.") for item in result.actions)
    assert result.report.report_version.report_type == "verified_execution"
    assert result.report.report_version.report_id == request.verified_report_id
    assert result.report.version_diff == request.version_diff_result.version_diff
    assert result.report.top_actions[1].dependencies == [result.report.top_actions[0].action_id]
    assert result.report.top_actions[1].action_id in result.report.top_actions[2].dependencies
    assert any("does not guarantee" in item.description for item in result.report.limitations)
    assert canonical_json_bytes(request.version_diff_input.parent_report) == parent_before
    assert len(result.report.findings) > len(request.version_diff_input.parent_report.findings)


def test_repeated_build_is_byte_deterministic_and_excludes_prohibited_gbp_content() -> None:
    request = execution_plan_request()
    first = build_execution_plan(request)
    second = build_execution_plan(request)
    assert canonical_json_bytes(first) == canonical_json_bytes(second)
    encoded = canonical_json_bytes(first).decode()
    assert "raw_payload" not in encoded
    assert "keywords" not in encoded
    assert first.report.first_party_performance.gbp.connection_state == "not_connected"
    assert first.report.data_coverage.full_evidence_coverage is False


def test_measurement_repair_is_hard_gate_for_later_actions() -> None:
    from test_v22_first_party_findings import gsc_value

    result = build_execution_plan(execution_plan_request(gsc=gsc_value(unhealthy=True)))
    assert result.actions[0].candidate_kind == "measurement_repair"
    assert result.actions[0].execution_gate == "blocked_until_measurement_ready"
    assert result.actions[0].primary_metric.evaluator == "source_ready"
    assert all(item.execution_gate == "waiting_for_action_1" for item in result.actions[1:])
    assert all(item.blocked_by_action_ids == [result.actions[0].action_id] for item in result.actions[1:])


@pytest.mark.anyio
async def test_official_gbp_stays_categorical_and_enables_full_coverage() -> None:
    from test_v22_first_party_findings import gbp_collection

    collected = await gbp_collection()
    result = build_execution_plan(execution_plan_request(gbp=(collected, collected.raw_payload)))
    gbp = result.report.first_party_performance.gbp
    assert gbp.connection_state == "verified"
    assert gbp.metrics == []
    assert result.report.data_coverage.full_evidence_coverage is True
    gbp_evidence = [item for item in result.report.evidence_index if item.source_type == "gbp"]
    assert gbp_evidence
    assert all(not isinstance(item.normalized_value, (int, float)) for item in gbp_evidence)
    encoded = canonical_json_bytes(result).decode()
    assert "raw_payload" not in encoded
    assert "keyword_rows" not in encoded


def test_strict_target_relation_adds_only_one_verified_guardrail() -> None:
    from test_v22_first_party_findings import gsc_value

    gsc = gsc_value()
    rows = list(gsc.current.queries.rows)
    rows[0] = rows[0].model_copy(update={"key": "  ＰＬＵＭＢＥＲ  "})
    gsc = gsc.model_copy(update={
        "current": gsc.current.model_copy(update={
            "queries": gsc.current.queries.model_copy(update={"rows": rows}),
        }),
    })
    result = build_execution_plan(execution_plan_request(gsc=gsc))
    guarded = [item for item in result.actions if item.guardrail_metric is not None]
    assert len(guarded) == 1
    guardrail = guarded[0].guardrail_metric
    assert guardrail.source_types == ["gsc"]
    assert guardrail.baseline_kind == "exact_value"
    assert guardrail.evaluator == "sample_floor_preserved"
    assert len(guardrail.evidence_ids) == 1


def test_direction_conflict_uses_conflict_cleared_primary_metric() -> None:
    from test_v22_cross_source_findings import aligned_values

    gsc, ga4 = aligned_values(conflict=True)
    result = build_execution_plan(execution_plan_request(gsc=gsc, ga4=ga4))
    action = result.actions[0]
    assert action.candidate_kind == "measurement_consistency"
    assert action.primary_metric.evaluator == "comparison_conflict_cleared"
    assert action.primary_metric.source_types == ["gsc", "ga4"]
    assert action.execution_gate == "ready"
    assert result.actions[1].blocked_by_action_ids == [action.action_id]


def test_tampered_and_resigned_upstream_result_is_rejected() -> None:
    request = execution_plan_request()
    changed = deepcopy(request.verified_reprioritization_result)
    changed.actions[0].verification_rank += 1
    selected_audit = next(
        item for item in changed.selection_audit
        if item.action_id == changed.actions[0].action_id
    )
    selected_audit.verification_rank += 1
    forged_diff_input = request.version_diff_input.model_copy(update={
        "verified_reprioritization_result": changed,
        "verified_reprioritization_result_checksum": request_digest(changed),
    })
    forged = request.model_copy(update={
        "verified_reprioritization_result": changed,
        "verified_reprioritization_result_checksum": request_digest(changed),
        "version_diff_input": forged_diff_input,
        "version_diff_input_checksum": request_digest(forged_diff_input),
    })
    with pytest.raises(ExecutionPlanError) as exc:
        build_execution_plan(forged)
    assert exc.value.error_code in {
        "V22_EXECUTION_PLAN_BINDING_INVALID", "V22_EXECUTION_PLAN_UPSTREAM_MISMATCH"
    }


def test_output_limit_fails_without_partial_result() -> None:
    request = execution_plan_request()
    request.limits.max_bytes = 1
    with pytest.raises(ExecutionPlanError) as exc:
        build_execution_plan(request)
    assert exc.value.error_code == "V22_EXECUTION_PLAN_LIMIT_EXCEEDED"
