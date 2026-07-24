"""Fixed remediation groups for the authoritative SearchTrust rule vector."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

from app.report_v21.rule_contract import ACTIVE_RULE_IDS


Priority = Literal["high", "medium", "low"]
EffortLevel = Literal["small", "medium", "large"]


@dataclass(frozen=True)
class ActionRequirement:
    action_key: str
    affected_layer: str
    candidate_finding_keys: tuple[str, ...]
    priority: Priority
    effort_level: EffortLevel
    task_goal: str

    @property
    def rule_ids(self) -> tuple[int, ...]:
        return tuple(int(key.removeprefix("rule_")) for key in self.candidate_finding_keys)

    def to_dify_input(self) -> dict[str, Any]:
        value = asdict(self)
        value["candidate_finding_keys"] = list(self.candidate_finding_keys)
        return value


ACTION_REQUIREMENTS: tuple[ActionRequirement, ...] = (
    ActionRequirement(
        "foundation_entity_scope",
        "foundation",
        ("rule_17", "rule_18", "rule_20"),
        "high",
        "medium",
        "Clarify which real business entity and qualified service scope the page represents.",
    ),
    ActionRequirement(
        "foundation_site_identity",
        "foundation",
        ("rule_19",),
        "high",
        "medium",
        "Standardize the business identity used across the site and this page.",
    ),
    ActionRequirement(
        "entity_presence_business_identity",
        "entity_presence",
        ("rule_21",),
        "high",
        "small",
        "Make the real business identity explicit in prominent visible page areas.",
    ),
    ActionRequirement(
        "entity_presence_contact_details",
        "entity_presence",
        ("rule_22", "rule_23"),
        "high",
        "small",
        "Add verified physical address and primary contact details where they are missing.",
    ),
    ActionRequirement(
        "entity_presence_service_operations",
        "entity_presence",
        ("rule_24", "rule_25"),
        "high",
        "small",
        "State the verified service area and operating hours clearly.",
    ),
    ActionRequirement(
        "entity_consistency_business_name",
        "entity_consistency",
        ("rule_26",),
        "high",
        "small",
        "Use one canonical business name that exactly matches the checked business record.",
    ),
    ActionRequirement(
        "entity_consistency_address",
        "entity_consistency",
        ("rule_27",),
        "high",
        "medium",
        "Use one canonical address format that exactly matches the checked business record.",
    ),
    ActionRequirement(
        "entity_consistency_phone",
        "entity_consistency",
        ("rule_28",),
        "high",
        "small",
        "Use one canonical primary phone number that exactly matches the checked business record.",
    ),
    ActionRequirement(
        "entity_consistency_service_area",
        "entity_consistency",
        ("rule_29",),
        "high",
        "medium",
        "Align the page service-area statement with the checked business record.",
    ),
    ActionRequirement(
        "specificity_verified_local_case",
        "specificity",
        ("rule_1", "rule_4", "rule_32", "rule_34", "rule_35", "rule_36"),
        "high",
        "medium",
        "Add a verified local service case with place, customer context, time, work performed, and outcome.",
    ),
    ActionRequirement(
        "specificity_concrete_service_copy",
        "specificity",
        ("rule_2", "rule_7"),
        "high",
        "medium",
        "Replace generic service language with concrete operational detail and first-person work description.",
    ),
    ActionRequirement(
        "specificity_original_visuals",
        "specificity",
        ("rule_6",),
        "medium",
        "large",
        "Replace reusable visual material with verified original service, team, equipment, or location imagery.",
    ),
    ActionRequirement(
        "specificity_contextual_cta",
        "specificity",
        ("rule_8",),
        "medium",
        "small",
        "Replace generic calls to action with service- and context-specific next steps.",
    ),
    ActionRequirement(
        "real_world_geographic_anchors",
        "real_world_connection",
        ("rule_3", "rule_30", "rule_31"),
        "high",
        "medium",
        "Add meaningful community, landmark, or operational geographic anchors.",
    ),
    ActionRequirement(
        "real_world_verifiable_proof",
        "real_world_connection",
        ("rule_11",),
        "high",
        "medium",
        "Add an externally verifiable trust clue that supports the page's real-world service claim.",
    ),
    ActionRequirement(
        "real_world_service_boundary",
        "real_world_connection",
        ("rule_33",),
        "medium",
        "small",
        "Define a clear and accurate service radius or operational boundary.",
    ),
    ActionRequirement(
        "accountability_operational_responsibility",
        "accountability",
        ("rule_9", "rule_12"),
        "medium",
        "medium",
        "Explain who is responsible for service delivery and how real-world outcomes are handled.",
    ),
    ActionRequirement(
        "accountability_constraints",
        "accountability",
        ("rule_10",),
        "medium",
        "medium",
        "Explain real service constraints, complex cases, exclusions, or escalation conditions.",
    ),
    ActionRequirement(
        "page_unique_value_differentiation",
        "page_unique_value",
        ("rule_13", "rule_14", "rule_16"),
        "medium",
        "large",
        "Create a page-specific value block that justifies separate indexing and cannot be reused unchanged.",
    ),
    ActionRequirement(
        "algorithm_current_trust_signals",
        "algorithm_fit",
        ("rule_15",),
        "low",
        "medium",
        "Strengthen current, verifiable trust signals instead of relying on legacy authority patterns.",
    ),
    ActionRequirement(
        "algorithm_review_service_detail",
        "algorithm_fit",
        ("rule_37",),
        "low",
        "medium",
        "Improve the review acquisition or presentation process so service-specific detail is available.",
    ),
    ActionRequirement(
        "algorithm_review_geographic_context",
        "algorithm_fit",
        ("rule_38",),
        "low",
        "medium",
        "Improve the review acquisition or presentation process so legitimate geographic context is available.",
    ),
    ActionRequirement(
        "algorithm_review_topic_alignment",
        "algorithm_fit",
        ("rule_39",),
        "low",
        "medium",
        "Align the review proof shown with the service topic and customer intent of this page.",
    ),
)


ACTION_REQUIREMENTS_BY_KEY: dict[str, ActionRequirement] = {
    requirement.action_key: requirement
    for requirement in ACTION_REQUIREMENTS
}


def serialize_action_requirements() -> dict[str, Any]:
    """Return the complete fixed catalog sent to the Dify workflow."""
    return {
        "requirements": [
            requirement.to_dify_input()
            for requirement in ACTION_REQUIREMENTS
        ]
    }


def active_action_requirements(
    rule_results: dict[int, bool],
    rule_applicability: dict[int, bool],
) -> dict[str, tuple[ActionRequirement, tuple[int, ...]]]:
    """Resolve active remediation groups from the final backend-owned vector."""
    active: dict[str, tuple[ActionRequirement, tuple[int, ...]]] = {}
    for requirement in ACTION_REQUIREMENTS:
        triggered = tuple(
            rule_id
            for rule_id in requirement.rule_ids
            if rule_results.get(rule_id) is True
            and rule_applicability.get(rule_id, True) is True
        )
        if triggered:
            active[requirement.action_key] = (requirement, triggered)
    return active


def _validate_catalog() -> None:
    action_keys = [item.action_key for item in ACTION_REQUIREMENTS]
    if len(action_keys) != len(set(action_keys)):
        raise RuntimeError("Action requirement keys must be unique.")

    mapped_rule_ids = [
        rule_id
        for requirement in ACTION_REQUIREMENTS
        for rule_id in requirement.rule_ids
    ]
    if len(mapped_rule_ids) != len(set(mapped_rule_ids)):
        raise RuntimeError("Each rule must belong to exactly one action requirement.")
    if set(mapped_rule_ids) != set(ACTIVE_RULE_IDS):
        missing = sorted(set(ACTIVE_RULE_IDS) - set(mapped_rule_ids))
        extra = sorted(set(mapped_rule_ids) - set(ACTIVE_RULE_IDS))
        raise RuntimeError(
            f"Action requirement catalog does not cover the active rule contract: "
            f"missing={missing}, extra={extra}"
        )


_validate_catalog()
