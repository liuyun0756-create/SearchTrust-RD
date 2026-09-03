from __future__ import annotations

from app.report_v22.copy_catalog import (
    ALL_PATTERNS,
    EXPLANATION_PATTERNS,
    SUPPORTED_TEMPLATE_KEYS,
    WHY_NOW_PATTERNS,
    pattern_definitions_for,
)
from app.report_v22.public_action_catalog import ACTION_TEMPLATES


def test_catalog_supports_every_current_public_action_template() -> None:
    assert set(SUPPORTED_TEMPLATE_KEYS) == set(ACTION_TEMPLATES)
    for template_key in ACTION_TEMPLATES:
        assert pattern_definitions_for(template_key, "why_now")
        assert pattern_definitions_for(template_key, "client_facing_explanation")


def test_pattern_keys_and_template_placeholders_are_exact_and_unique() -> None:
    keys = [definition.spec.pattern_key for definition in ALL_PATTERNS]
    assert len(keys) == len(set(keys))
    for definition in ALL_PATTERNS:
        assert definition.placeholder_names == tuple(
            slot.slot_name for slot in definition.spec.slots
        )


def test_why_now_uses_supported_findings_only() -> None:
    for definition in WHY_NOW_PATTERNS:
        assert definition.spec.purpose == "why_now"
        assert [slot.allowed_fact_types for slot in definition.spec.slots] == [
            ["finding_statement"]
        ]


def test_explanations_require_targets_completion_and_limitations() -> None:
    for definition in EXPLANATION_PATTERNS:
        slots = {slot.slot_name: slot for slot in definition.spec.slots}
        assert set(slots) == {"targets", "completion", "limitations"}
        assert slots["targets"].min_items == 1
        assert slots["completion"].allowed_fact_types == ["definition_of_done"]
        assert slots["limitations"].allowed_fact_types == ["required_limitation"]
        assert slots["limitations"].min_items == 1


def test_catalog_does_not_contain_outcome_promises() -> None:
    text = " ".join(
        definition.template + " " + definition.spec.selection_hint
        for definition in ALL_PATTERNS
    ).casefold()
    forbidden = (
        "guaranteed ranking",
        "guarantee rankings",
        "traffic increase",
        "lead increase",
        "revenue increase",
    )
    assert not any(value in text for value in forbidden)
