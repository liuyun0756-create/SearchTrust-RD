from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.report_v22.copy_errors import (
    PublicCopyInputError,
    PublicCopyOutputError,
    PublicCopyRetryExhaustedError,
)
from app.report_v22.copy_models import (
    CopyActionInput,
    CopyActionSelection,
    CopyFact,
    CopyPatternSlotSpec,
    CopyPatternSpec,
    CopyRequestV1,
    CopyResponseV1,
    PatternSelection,
)


DIGEST = "sha256:" + "a" * 64


def fact(sequence: int) -> CopyFact:
    return CopyFact(
        fact_id=f"cf_{sequence:024x}",
        action_id=f"ac_action_{sequence}",
        source_finding_ids=[f"fn_finding_{sequence}"],
        source_path="finding.statement",
        fact_type="finding_statement",
        value="A saved finding.",
        allowed_purposes=["why_now"],
    )


def pattern(purpose: str) -> CopyPatternSpec:
    name = "finding" if purpose == "why_now" else "target"
    fact_type = "finding_statement" if purpose == "why_now" else "target_url"
    return CopyPatternSpec(
        pattern_key=f"{purpose}.safe.v1",
        purpose=purpose,
        compatible_template_keys=["restore_site_access_indexing"],
        selection_hint="Use a supported saved fact.",
        slots=[
            CopyPatternSlotSpec(
                slot_name=name,
                allowed_fact_types=[fact_type],
                min_items=1,
                max_items=1,
            )
        ],
        max_rendered_chars=1_000,
    )


def action(sequence: int) -> CopyActionInput:
    finding = fact(sequence)
    target = CopyFact(
        fact_id=f"cf_{sequence + 10:024x}",
        action_id=finding.action_id,
        source_finding_ids=[f"fn_finding_{sequence}"],
        source_path="target.url.deadbeef.url",
        fact_type="target_url",
        value=f"https://example.test/page-{sequence}",
        allowed_purposes=["client_facing_explanation"],
    )
    limitation = CopyFact(
        fact_id=f"cf_{sequence + 20:024x}",
        action_id=finding.action_id,
        source_finding_ids=[],
        source_path="action.required_limitation.0",
        fact_type="required_limitation",
        value="Do not promise a search outcome.",
        allowed_purposes=["client_facing_explanation"],
    )
    explanation = pattern("client_facing_explanation").model_copy(
        update={
            "slots": [
                CopyPatternSlotSpec(
                    slot_name="target",
                    allowed_fact_types=["target_url"],
                    min_items=1,
                    max_items=10,
                ),
                CopyPatternSlotSpec(
                    slot_name="limitations",
                    allowed_fact_types=["required_limitation"],
                    min_items=1,
                    max_items=20,
                ),
            ]
        }
    )
    return CopyActionInput(
        action_id=finding.action_id,
        sequence=sequence,
        template_key="restore_site_access_indexing",
        template_version="1.0.0",
        finding_ids=[f"fn_finding_{sequence}"],
        facts=[finding, target, limitation],
        why_now_patterns=[pattern("why_now")],
        explanation_patterns=[explanation],
        required_limitation_ids=[limitation.fact_id],
    )


def request() -> CopyRequestV1:
    return CopyRequestV1(
        findings_checksum=DIGEST,
        action_plan_checksum=DIGEST,
        actions=[action(1), action(2), action(3)],
    )


def response() -> CopyResponseV1:
    items = []
    for item in request().actions:
        facts = {fact.fact_type: fact.fact_id for fact in item.facts}
        items.append(
            CopyActionSelection(
                action_id=item.action_id,
                sequence=item.sequence,
                why_now=PatternSelection(
                    pattern_key="why_now.safe.v1",
                    slot_bindings={"finding": [facts["finding_statement"]]},
                ),
                client_facing_explanation=PatternSelection(
                    pattern_key="client_facing_explanation.safe.v1",
                    slot_bindings={
                        "target": [facts["target_url"]],
                        "limitations": [facts["required_limitation"]],
                    },
                ),
            )
        )
    return CopyResponseV1(action_plan_checksum=DIGEST, actions=items)


def test_request_and_response_require_exactly_three_ordered_unique_actions() -> None:
    value = request().model_dump(mode="python")
    value["actions"] = value["actions"][:2]
    with pytest.raises(ValidationError):
        CopyRequestV1.model_validate(value)

    value = response().model_dump(mode="python")
    value["actions"][1]["sequence"] = 3
    with pytest.raises(ValidationError):
        CopyResponseV1.model_validate(value)


def test_contract_models_reject_extra_fields_and_type_coercion() -> None:
    value = response().model_dump(mode="python")
    value["free_text"] = "An unsupported claim"
    with pytest.raises(ValidationError):
        CopyResponseV1.model_validate(value)

    value = request().model_dump(mode="python")
    value["actions"][0]["sequence"] = "1"
    with pytest.raises(ValidationError):
        CopyRequestV1.model_validate(value)


def test_fact_ownership_and_limitation_references_are_strict() -> None:
    value = action(1).model_dump(mode="python")
    value["facts"][0]["action_id"] = "ac_action_2"
    with pytest.raises(ValidationError):
        CopyActionInput.model_validate(value)

    value = action(1).model_dump(mode="python")
    value["required_limitation_ids"] = [value["facts"][0]["fact_id"]]
    with pytest.raises(ValidationError):
        CopyActionInput.model_validate(value)


def test_slot_bindings_reject_duplicate_or_empty_fact_lists() -> None:
    with pytest.raises(ValidationError):
        PatternSelection(pattern_key="why_now.safe.v1", slot_bindings={"finding": []})
    with pytest.raises(ValidationError):
        PatternSelection(
            pattern_key="why_now.safe.v1",
            slot_bindings={"finding": ["cf_000000000000000000000001"] * 2},
        )


def test_copy_errors_are_stable_retry_classified_and_payload_free() -> None:
    local = PublicCopyInputError("CHECKSUM_MISMATCH")
    output = PublicCopyOutputError("REFERENCE_INVALID", ["actions[0].why_now"])
    exhausted = PublicCopyRetryExhaustedError([output.error_code] * 5)

    assert local.error_code == "V22_COPY_CHECKSUM_MISMATCH"
    assert local.retryable is False
    assert output.error_code == "V22_COPY_REFERENCE_INVALID"
    assert output.retryable is True
    assert output.details == ("actions[0].why_now",)
    assert exhausted.error_code == "V22_COPY_RETRY_EXHAUSTED"
    assert exhausted.retryable is True
    assert len(exhausted.details) == 3
    assert "unsupported claim" not in str(output).lower()
