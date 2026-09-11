from __future__ import annotations

import json
import math

import pytest
from hypothesis import given, strategies as st
from pydantic import ValidationError

from app.jobs_v22.cost_models import ALLOWED_COUNTER_KEYS, CostCountersV1, MAX_COUNTER_VALUE
from app.report_v22.models import SourceCoverage, TargetMarket
from property.strategies import COUNTER_VALUES, SAFE_TEXT


@given(st.sampled_from((math.nan, math.inf, -math.inf)))
def test_target_market_rejects_every_nonfinite_coordinate(value: float) -> None:
    with pytest.raises(ValidationError):
        TargetMarket.model_validate_json(
            json.dumps({
                "display_name": "Synthetic market",
                "country_code": "US",
                "latitude": value,
                "longitude": 0.0,
            })
        )


@given(SAFE_TEXT)
def test_strict_models_reject_unknown_fields(field_name: str) -> None:
    key = f"unexpected_{field_name.replace(' ', '_')}"
    with pytest.raises(ValidationError):
        TargetMarket.model_validate({
            "display_name": "Synthetic market",
            "country_code": "US",
            key: "value",
        })


@given(st.integers(min_value=0, max_value=1_000_000).map(str))
def test_strict_models_reject_numeric_string_coercion(value: str) -> None:
    with pytest.raises(ValidationError):
        SourceCoverage.model_validate({
            "source_type": "site",
            "health_status": "healthy",
            "identity_match_status": "matched",
            "snapshot_ids": [],
            "checked_items": value,
            "available_items": 0,
            "coverage_summary": "Synthetic coverage.",
            "limitations": [],
        })


@given(COUNTER_VALUES)
def test_cost_counter_contract_rejects_values_above_declared_limit(extra: int) -> None:
    counters = {key: 0 for key in ALLOWED_COUNTER_KEYS}
    counters.update(
        cost_schema_version=1,
        cost_ledger_revision=1,
        pricing_revision=1,
        provider_attempts_total=MAX_COUNTER_VALUE + 1 + extra,
    )
    with pytest.raises(ValidationError):
        CostCountersV1(counters)
