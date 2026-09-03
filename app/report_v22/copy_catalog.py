"""Versioned backend-owned sentence catalog for controlled v2.2 copy."""

from __future__ import annotations

from dataclasses import dataclass
from string import Formatter

from app.report_v22.copy_models import CopyPatternSlotSpec, CopyPatternSpec, CopyPurpose
from app.report_v22.public_action_catalog import ACTION_TEMPLATES


CATALOG_VERSION = "v22_english_action_copy_v1"
SUPPORTED_TEMPLATE_KEYS = tuple(sorted(ACTION_TEMPLATES))


@dataclass(frozen=True)
class CopyPatternDefinition:
    spec: CopyPatternSpec
    template: str

    @property
    def placeholder_names(self) -> tuple[str, ...]:
        return tuple(
            field_name
            for _, field_name, _, _ in Formatter().parse(self.template)
            if field_name is not None
        )


def _slot(
    name: str,
    fact_types: tuple[str, ...],
    *,
    minimum: int,
    maximum: int,
) -> CopyPatternSlotSpec:
    return CopyPatternSlotSpec(
        slot_name=name,
        allowed_fact_types=list(fact_types),
        min_items=minimum,
        max_items=maximum,
    )


def _definition(
    key: str,
    purpose: CopyPurpose,
    hint: str,
    slots: tuple[CopyPatternSlotSpec, ...],
    template: str,
) -> CopyPatternDefinition:
    return CopyPatternDefinition(
        spec=CopyPatternSpec(
            pattern_key=key,
            purpose=purpose,
            compatible_template_keys=list(SUPPORTED_TEMPLATE_KEYS),
            selection_hint=hint,
            slots=list(slots),
            max_rendered_chars=200_000,
        ),
        template=template,
    )


WHY_NOW_PATTERNS = (
    _definition(
        "why_now.evidence_finding.v1",
        "why_now",
        "Lead with one supported finding from the saved evidence.",
        (_slot("finding", ("finding_statement",), minimum=1, maximum=1),),
        "The saved evidence makes this action timely: {finding}",
    ),
    _definition(
        "why_now.current_finding.v1",
        "why_now",
        "Describe why the current supported finding makes the action relevant.",
        (_slot("finding", ("finding_statement",), minimum=1, maximum=1),),
        "This action is relevant now because the current finding is: {finding}",
    ),
)


_TARGET_FACT_TYPES = (
    "target_url",
    "target_query",
    "target_page_type",
    "target_gbp_field",
    "target_site",
)


EXPLANATION_PATTERNS = (
    _definition(
        "explanation.targets_completion_limits.v1",
        "client_facing_explanation",
        "State the exact supported scope, completion condition and every required limitation.",
        (
            _slot("targets", _TARGET_FACT_TYPES, minimum=1, maximum=5_000),
            _slot("completion", ("definition_of_done",), minimum=1, maximum=20),
            _slot("limitations", ("required_limitation",), minimum=1, maximum=20),
        ),
        "Focus on {targets}. Completion is defined by {completion} Keep these limits in view: {limitations}",
    ),
    _definition(
        "explanation.completion_targets_limits.v1",
        "client_facing_explanation",
        "Lead with the supported target, then state completion and every required limitation.",
        (
            _slot("targets", _TARGET_FACT_TYPES, minimum=1, maximum=5_000),
            _slot("completion", ("definition_of_done",), minimum=1, maximum=20),
            _slot("limitations", ("required_limitation",), minimum=1, maximum=20),
        ),
        "Use {targets} as the exact scope. The completion condition is {completion} Keep these limits in view: {limitations}",
    ),
)


ALL_PATTERNS = WHY_NOW_PATTERNS + EXPLANATION_PATTERNS
PATTERNS_BY_KEY = {definition.spec.pattern_key: definition for definition in ALL_PATTERNS}


def pattern_definitions_for(
    template_key: str,
    purpose: CopyPurpose,
) -> tuple[CopyPatternDefinition, ...]:
    """Return the fixed compatible patterns in stable catalog order."""
    source = WHY_NOW_PATTERNS if purpose == "why_now" else EXPLANATION_PATTERNS
    return tuple(
        definition
        for definition in source
        if template_key in definition.spec.compatible_template_keys
    )


def pattern_definition(pattern_key: str) -> CopyPatternDefinition | None:
    return PATTERNS_BY_KEY.get(pattern_key)
