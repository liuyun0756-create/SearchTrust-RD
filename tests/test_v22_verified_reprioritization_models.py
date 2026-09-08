from copy import deepcopy

import pytest
from pydantic import ValidationError

from app.report_v22.verified_reprioritization_models import VerifiedReprioritizationInput
from verified_reprioritization_helpers import verified_request


def test_input_contract_is_strict_and_rejects_nonfinite_values() -> None:
    raw = verified_request().model_dump(mode="python")
    raw["unexpected"] = True
    with pytest.raises(ValidationError):
        VerifiedReprioritizationInput.model_validate(raw)

    raw = verified_request().model_dump(mode="python")
    raw["first_party_result"]["rule_evaluations"][0]["impact_score"] = float("nan")
    with pytest.raises(ValidationError):
        VerifiedReprioritizationInput.model_validate(raw)


def test_model_copy_does_not_mutate_original_fixture() -> None:
    value = verified_request()
    before = deepcopy(value)
    value.model_copy(update={"planning_date": value.planning_date})
    assert value == before
