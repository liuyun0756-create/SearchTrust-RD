from __future__ import annotations

from copy import deepcopy
from datetime import date
import json

import pytest

from app.jobs_v22.digest import canonical_json_bytes
from app.report_v22.action_models import PublicActionPlanInput
from app.report_v22.actions import build_public_action_plan
from app.report_v22.copy_contract import (
    COPY_OUTPUT_KEY,
    build_copy_request,
    validate_and_render_copy,
)
from app.report_v22.copy_errors import PublicCopyInputError, PublicCopyOutputError
from app.report_v22.copy_models import PublicCopyLimits
from v22_copy_helpers import copy_request, public_findings, valid_response


def test_native_and_json_string_responses_render_the_same_three_actions() -> None:
    request_value = copy_request()
    outputs = valid_response(request_value)
    native = validate_and_render_copy(request_value, outputs)
    encoded = validate_and_render_copy(
        request_value,
        {COPY_OUTPUT_KEY: json.dumps(outputs[COPY_OUTPUT_KEY])},
    )

    assert native == encoded
    assert len(native.actions) == 3
    assert [action.action_id for action in native.actions] == [
        action.action_id for action in request_value.actions
    ]
    assert all(action.why_now for action in native.actions)
    assert all(action.client_facing_explanation for action in native.actions)


def test_json_string_with_duplicate_object_keys_is_rejected() -> None:
    request_value = copy_request()
    value = json.dumps(valid_response(request_value)[COPY_OUTPUT_KEY])
    duplicate = value[:-1] + ',"language":"en"}'

    with pytest.raises(PublicCopyOutputError) as exc:
        validate_and_render_copy(request_value, {COPY_OUTPUT_KEY: duplicate})
    assert exc.value.error_code == "V22_COPY_OUTPUT_INVALID"


def test_rendered_numbers_urls_and_findings_come_only_from_backend_facts() -> None:
    request_value = copy_request()
    result = validate_and_render_copy(request_value, valid_response(request_value))

    for request_action, rendered in zip(request_value.actions, result.actions, strict=True):
        facts = {fact.fact_id: fact.value for fact in request_action.facts}
        for fact_id in rendered.why_now_fact_ids:
            assert facts[fact_id] in rendered.why_now
        for fact_id in rendered.explanation_fact_ids:
            assert facts[fact_id] in rendered.client_facing_explanation


@pytest.mark.parametrize(
    "value",
    [
        None,
        {},
        {COPY_OUTPUT_KEY: None},
        {COPY_OUTPUT_KEY: "not-json"},
        {COPY_OUTPUT_KEY: "```json\n{}\n```"},
        {COPY_OUTPUT_KEY: {}, "extra": {}},
    ],
)
def test_missing_malformed_or_markdown_outputs_are_rejected(value) -> None:
    with pytest.raises(PublicCopyOutputError) as exc:
        validate_and_render_copy(copy_request(), value)
    assert exc.value.retryable is True
    assert exc.value.error_code in {
        "V22_COPY_OUTPUT_MISSING",
        "V22_COPY_OUTPUT_INVALID",
    }


def test_extra_free_text_and_modified_literal_fields_are_rejected_before_rendering() -> None:
    request_value = copy_request()
    outputs = valid_response(request_value)
    outputs[COPY_OUTPUT_KEY]["actions"][0]["why_now"]["text"] = (
        "Rankings will improve by 25% at https://invented.test/"
    )

    with pytest.raises(PublicCopyOutputError) as exc:
        validate_and_render_copy(request_value, outputs)
    assert exc.value.error_code == "V22_COPY_OUTPUT_INVALID"
    assert exc.value.details == ("actions.0.why_now.<unexpected>",)
    assert "25" not in str(exc.value)
    assert "invented" not in str(exc.value)

    outputs = valid_response(request_value)
    secret_key = "SECRET-CUSTOMER-ADDRESS-555-0100"
    outputs[COPY_OUTPUT_KEY][secret_key] = "private value"
    with pytest.raises(PublicCopyOutputError) as exc:
        validate_and_render_copy(request_value, outputs)
    assert secret_key not in ".".join(exc.value.details)
    assert "private" not in ".".join(exc.value.details)


def test_language_and_action_plan_checksum_mismatches_have_specific_codes() -> None:
    request_value = copy_request()
    outputs = valid_response(request_value)
    outputs[COPY_OUTPUT_KEY]["language"] = "zh"
    with pytest.raises(PublicCopyOutputError) as exc:
        validate_and_render_copy(request_value, outputs)
    assert exc.value.error_code == "V22_COPY_LANGUAGE_INVALID"

    outputs = valid_response(request_value)
    outputs[COPY_OUTPUT_KEY]["action_plan_checksum"] = "sha256:" + "0" * 64
    with pytest.raises(PublicCopyOutputError) as exc:
        validate_and_render_copy(request_value, outputs)
    assert exc.value.error_code == "V22_COPY_CHECKSUM_MISMATCH"


def test_missing_duplicate_or_reordered_actions_are_rejected() -> None:
    request_value = copy_request()
    outputs = valid_response(request_value)
    outputs[COPY_OUTPUT_KEY]["actions"] = outputs[COPY_OUTPUT_KEY]["actions"][:2]
    with pytest.raises(PublicCopyOutputError):
        validate_and_render_copy(request_value, outputs)

    outputs = valid_response(request_value)
    outputs[COPY_OUTPUT_KEY]["actions"][1] = deepcopy(
        outputs[COPY_OUTPUT_KEY]["actions"][0]
    )
    with pytest.raises(PublicCopyOutputError):
        validate_and_render_copy(request_value, outputs)

    outputs = valid_response(request_value)
    outputs[COPY_OUTPUT_KEY]["actions"].reverse()
    with pytest.raises(PublicCopyOutputError):
        validate_and_render_copy(request_value, outputs)


def test_unknown_and_cross_action_fact_references_are_rejected() -> None:
    request_value = copy_request()
    outputs = valid_response(request_value)
    bindings = outputs[COPY_OUTPUT_KEY]["actions"][0]["why_now"]["slot_bindings"]
    bindings["finding"] = ["cf_000000000000000000000000"]
    with pytest.raises(PublicCopyOutputError) as exc:
        validate_and_render_copy(request_value, outputs)
    assert exc.value.error_code == "V22_COPY_REFERENCE_INVALID"

    outputs = valid_response(request_value)
    foreign = request_value.actions[1].facts[0].fact_id
    bindings = outputs[COPY_OUTPUT_KEY]["actions"][0]["why_now"]["slot_bindings"]
    bindings["finding"] = [foreign]
    with pytest.raises(PublicCopyOutputError) as exc:
        validate_and_render_copy(request_value, outputs)
    assert exc.value.error_code == "V22_COPY_REFERENCE_INVALID"


def test_unknown_pattern_and_wrong_fact_type_are_rejected() -> None:
    request_value = copy_request()
    outputs = valid_response(request_value)
    outputs[COPY_OUTPUT_KEY]["actions"][0]["why_now"]["pattern_key"] = (
        "why_now.unknown.v1"
    )
    with pytest.raises(PublicCopyOutputError) as exc:
        validate_and_render_copy(request_value, outputs)
    assert exc.value.error_code == "V22_COPY_REFERENCE_INVALID"

    outputs = valid_response(request_value)
    action = request_value.actions[0]
    limitation = next(
        fact.fact_id for fact in action.facts if fact.fact_type == "required_limitation"
    )
    outputs[COPY_OUTPUT_KEY]["actions"][0]["why_now"]["slot_bindings"][
        "finding"
    ] = [limitation]
    with pytest.raises(PublicCopyOutputError) as exc:
        validate_and_render_copy(request_value, outputs)
    assert exc.value.error_code == "V22_COPY_REFERENCE_INVALID"

def test_all_targets_completion_conditions_and_limitations_are_mandatory() -> None:
    request_value = copy_request()
    outputs = valid_response(request_value)
    bindings = outputs[COPY_OUTPUT_KEY]["actions"][0][
        "client_facing_explanation"
    ]["slot_bindings"]
    bindings["targets"] = bindings["targets"][:1]

    with pytest.raises(PublicCopyOutputError) as exc:
        validate_and_render_copy(request_value, outputs)
    assert exc.value.error_code == "V22_COPY_REFERENCE_INVALID"
    assert exc.value.details == (
        "client_facing_explanation",
        "required_facts",
    )

    outputs = valid_response(request_value)
    bindings = outputs[COPY_OUTPUT_KEY]["actions"][0][
        "client_facing_explanation"
    ]["slot_bindings"]
    bindings["limitations"] = []
    with pytest.raises(PublicCopyOutputError):
        validate_and_render_copy(request_value, outputs)


def test_backend_owned_non_english_fact_values_are_preserved_verbatim() -> None:
    findings = public_findings()
    selected = next(
        finding
        for finding in findings.findings
        if finding.rule_id.endswith("site_explicit_noindex")
    )
    selected.statement = "商家在已保存样本中的位置是 25。"
    plan = build_public_action_plan(
        PublicActionPlanInput(
            findings_result=findings,
            planning_date=date(2026, 9, 3),
        )
    )
    request_value = build_copy_request(findings, plan)
    outputs = valid_response(request_value)
    action_index = next(
        index
        for index, action in enumerate(request_value.actions)
        if selected.finding_id in action.finding_ids
    )
    response_action = outputs[COPY_OUTPUT_KEY]["actions"][action_index]
    fact = next(
        fact
        for fact in request_value.actions[action_index].facts
        if fact.source_finding_ids == [selected.finding_id]
        and fact.fact_type == "finding_statement"
    )
    response_action["why_now"]["slot_bindings"]["finding"] = [fact.fact_id]

    rendered = validate_and_render_copy(request_value, outputs)
    assert selected.statement in rendered.actions[action_index].why_now


def test_response_and_rendered_size_limits_are_enforced() -> None:
    request_value = copy_request()
    outputs = valid_response(request_value)
    with pytest.raises(PublicCopyOutputError) as exc:
        validate_and_render_copy(
            request_value,
            outputs,
            limits=PublicCopyLimits(max_output_bytes=1),
        )
    assert exc.value.error_code == "V22_COPY_LIMIT_EXCEEDED"

    with pytest.raises(PublicCopyOutputError) as exc:
        validate_and_render_copy(
            request_value,
            outputs,
            limits=PublicCopyLimits(max_rendered_chars=1),
        )
    assert exc.value.error_code == "V22_COPY_LIMIT_EXCEEDED"


def test_request_fact_tampering_is_a_local_nonretryable_failure() -> None:
    request_value = copy_request()
    before = canonical_json_bytes(request_value)
    request_value.actions[0].facts[0].value = "Changed after binding"
    tampered = canonical_json_bytes(request_value)
    assert tampered != before

    with pytest.raises(PublicCopyInputError) as exc:
        validate_and_render_copy(request_value, valid_response(request_value))
    assert exc.value.error_code == "V22_COPY_CHECKSUM_MISMATCH"
    assert exc.value.retryable is False
    assert canonical_json_bytes(request_value) == tampered


def test_request_pattern_tampering_is_a_local_catalog_failure() -> None:
    request_value = copy_request()
    request_value.actions[0].why_now_patterns[0].selection_hint = "Injected hint"

    with pytest.raises(PublicCopyInputError) as exc:
        validate_and_render_copy(request_value, valid_response(request_value))
    assert exc.value.error_code == "V22_COPY_CATALOG_INVALID"
    assert exc.value.retryable is False


def test_runtime_request_limits_are_rechecked_before_rendering() -> None:
    request_value = copy_request()
    with pytest.raises(PublicCopyInputError) as exc:
        validate_and_render_copy(
            request_value,
            valid_response(request_value),
            limits=PublicCopyLimits(max_facts_per_action=1),
        )
    assert exc.value.error_code == "V22_COPY_LIMIT_EXCEEDED"
