"""Fixed remediation groups for the authoritative SearchTrust rule vector."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

from app.report_v21.rule_contract import ACTIVE_RULE_IDS
from app.report_v21.scoring import RULE_FINDING_LABELS


Priority = Literal["high", "medium", "low"]
EffortLevel = Literal["small", "medium", "large"]


RULE_REMEDIATION_GUIDANCE: dict[int, str] = {
    1: "Add place-specific details that would need to change if the city or service area changed.",
    2: "Replace broad service descriptions with concrete service-specific detail.",
    3: "Add a verified neighborhood, landmark, facility, or operational geographic anchor.",
    4: "Add a verified date, time reference, recent activity marker, or service timeline.",
    6: "Use verified original imagery from the business, team, equipment, or real work.",
    7: "Describe the team's concrete actions and service process in first-person operational language.",
    8: "Use calls to action that name the service need and the next step for this page.",
    9: "Explain who handles the request and what responsibility the business takes during delivery.",
    10: "Describe verified constraints, complex cases, exclusions, or escalation conditions.",
    11: "Add a verifiable external clue that supports the page's real-world service claim.",
    12: "State who is accountable for the outcome and how follow-up or correction is handled.",
    13: "Replace reusable cluster copy with page-specific material that cannot be repeated unchanged.",
    14: "Add a distinct user purpose or value block that justifies this page being indexed separately.",
    15: "Add current, verifiable trust signals that do not depend on historical authority alone.",
    16: "Give the page a useful role for customers beyond covering a search query.",
    17: "Clarify the real business entity and the qualified service scope represented by the page.",
    18: "Limit service claims to verified business categories or establish the missing category relationship.",
    19: "Use one canonical business identity consistently across the page and related site areas.",
    20: "Clarify the verified qualification that makes the entity eligible for the targeted service query.",
    21: "Make the real business name and identity explicit in prominent visible page areas.",
    22: "Add the verified physical address in an appropriate visible contact location.",
    23: "Add the verified primary phone number in prominent contact and call-to-action locations.",
    24: "State the verified service area clearly and consistently.",
    25: "Publish verified operating or availability hours where customers can find them.",
    26: "Use the exact canonical business name from the checked GBP record.",
    27: "Use one canonical address format that exactly matches the checked GBP record.",
    28: "Use one canonical primary phone number that exactly matches the checked GBP record.",
    29: "Align the page service-area statement with the checked GBP record.",
    30: "Add verified community-level locations that are genuinely served.",
    31: "Add a verified landmark or non-administrative place reference relevant to service delivery.",
    32: "Add factual local operating context tied to the checked service area.",
    33: "Define an accurate service radius, boundary, or surrounding-community coverage statement.",
    34: "Add one verified service case with the problem, response, and outcome.",
    35: "Describe the verified customer situation or operating context behind a service example.",
    36: "Add meaningful verified time context to the service example or activity evidence.",
    37: "Collect or present reviews that name the specific service performed.",
    38: "Collect or present reviews that include legitimate service-area or place context.",
    39: "Present review proof whose service topic matches the focus of this page.",
}


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
        value["candidate_findings"] = [
            {
                "finding_key": finding_key,
                "finding_label": RULE_FINDING_LABELS[rule_id],
                "required_change": RULE_REMEDIATION_GUIDANCE[rule_id],
            }
            for finding_key in self.candidate_finding_keys
            for rule_id in (int(finding_key.removeprefix("rule_")),)
        ]
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


def action_finding_details(rule_ids: tuple[int, ...] | list[int]) -> tuple[list[str], list[str]]:
    """Return deterministic user-facing findings and remediation requirements."""
    addressed_findings = [
        RULE_FINDING_LABELS[rule_id]
        for rule_id in rule_ids
        if rule_id in RULE_FINDING_LABELS
    ]
    required_changes = [
        RULE_REMEDIATION_GUIDANCE[rule_id]
        for rule_id in rule_ids
        if rule_id in RULE_REMEDIATION_GUIDANCE
    ]
    return addressed_findings, required_changes


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
    if set(RULE_REMEDIATION_GUIDANCE) != set(ACTIVE_RULE_IDS):
        missing = sorted(set(ACTIVE_RULE_IDS) - set(RULE_REMEDIATION_GUIDANCE))
        extra = sorted(set(RULE_REMEDIATION_GUIDANCE) - set(ACTIVE_RULE_IDS))
        raise RuntimeError(
            "Rule remediation guidance must cover the active rule contract: "
            f"missing={missing}, extra={extra}"
        )


_validate_catalog()
