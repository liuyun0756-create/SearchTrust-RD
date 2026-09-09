from copy import deepcopy

import pytest
from pydantic import ValidationError

from app.report_v22.execution_plan_models import ExecutionMetric
from execution_plan_helpers import execution_plan_request


def test_contracts_are_strict_and_limits_are_bounded() -> None:
    value = execution_plan_request()
    raw = value.model_dump(mode="json")
    raw["unexpected"] = True
    with pytest.raises(ValidationError):
        type(value).model_validate(raw)
    assert value.limits.max_bytes == 25_000_000


def test_value_baseline_requires_evidence_and_guardrail_evaluator_is_restricted() -> None:
    with pytest.raises(ValidationError):
        ExecutionMetric(
            metric_key="verified_gsc_impressions", role="guardrail",
            baseline_kind="exact_value", baseline="impressions=10",
            success_condition="Preserve the sample.", source_types=["gsc"],
            evaluator="sample_floor_preserved",
        )
    metric = deepcopy(execution_plan_request().verified_reprioritization_result.actions[0])
    assert metric.sequence == 1
