"""Deterministic, client-safe presentation copy for report delivery."""

from __future__ import annotations

from dataclasses import dataclass


CATALOG_VERSION = "v22_client_delivery_v1"


@dataclass(frozen=True)
class ClientDeliveryTemplate:
    decision_headline: str
    business_impact: str
    opportunity: str
    action_title: str
    why_now: str
    expected_result: str
    evidence_subject: str
    evidence_observation: str
    evidence_relevance: str


CLIENT_DELIVERY_TEMPLATES: dict[str, ClientDeliveryTemplate] = {
    "restore_site_access_indexing": ClientDeliveryTemplate(
        decision_headline="Restore access to the priority pages first",
        business_impact="A checked priority page has an access or indexing condition that can prevent later improvements from being evaluated reliably.",
        opportunity="Resolve the confirmed condition, then verify the same page again before investing in wider optimization.",
        action_title="Restore search eligibility for priority pages",
        why_now="The checked page condition needs to be resolved before later optimization is assessed.",
        expected_result="A fresh website check confirms the agreed priority pages are available and eligible for search.",
        evidence_subject="Priority page check",
        evidence_observation="The saved website check found an access or indexing condition on a priority page.",
        evidence_relevance="This condition must be resolved before later page improvements can be evaluated reliably.",
    ),
    "differentiate_page_titles": ClientDeliveryTemplate(
        decision_headline="Clarify the role of each priority page",
        business_impact="Checked pages use repeated titles, making their distinct purpose less clear to searchers and search systems.",
        opportunity="Give each page a distinct, accurate title and confirm the change with a fresh website check.",
        action_title="Differentiate the priority page titles",
        why_now="The checked pages do not yet communicate a sufficiently distinct purpose through their titles.",
        expected_result="A fresh website check confirms each priority page has an approved, distinct title.",
        evidence_subject="Page title check",
        evidence_observation="The saved website check found the same normalized title across selected pages.",
        evidence_relevance="Distinct titles make the intended role of each page clearer and create a clean basis for review.",
    ),
    "review_market_visibility": ClientDeliveryTemplate(
        decision_headline="Close the confirmed market visibility gap",
        business_impact="The saved market sample shows a visibility gap for the agreed search context, without proving a single cause.",
        opportunity="Protect the comparison basis, make only evidence-supported changes, and repeat the same market sample.",
        action_title="Review the confirmed market visibility gap",
        why_now="The agreed market sample contains a visibility difference that should be rechecked on a comparable basis.",
        expected_result="A fresh comparable market sample shows whether the selected visibility condition changed.",
        evidence_subject="Confirmed market sample",
        evidence_observation="The saved market sample recorded a visibility difference in the agreed search context.",
        evidence_relevance="This establishes the comparison to revisit, but it does not by itself identify a ranking cause.",
    ),
    "close_page_type_gap": ClientDeliveryTemplate(
        decision_headline="Decide whether the missing page type is a real growth gap",
        business_impact="The confirmed competitor sample contains a useful page type that was not found in the client sample.",
        opportunity="Validate the underlying customer need, then approve a distinct useful page or document a no-build decision.",
        action_title="Resolve the confirmed page-type gap",
        why_now="The sampled difference may represent an unmet customer need, but it should be validated before anything is copied or built.",
        expected_result="The client has an approved useful page or a documented decision not to build one.",
        evidence_subject="Page-type comparison",
        evidence_observation="The confirmed competitor sample included a page type that was not found in the client sample.",
        evidence_relevance="This is a decision prompt, not proof that the client should copy a competitor asset.",
    ),
    "align_public_gbp": ClientDeliveryTemplate(
        decision_headline="Align the public business identity",
        business_impact="A checked website and public business profile field is missing, partially aligned, or inconsistent.",
        opportunity="Confirm the authoritative value with the client, update only approved channels, and check both again.",
        action_title="Align the website and public business profile",
        why_now="Customers and search systems should encounter the same approved business information across public channels.",
        expected_result="Fresh public checks show the approved field is present and aligned across the website and business profile.",
        evidence_subject="Public business information",
        evidence_observation="The saved public checks found a missing, partial, or inconsistent business field across the two channels.",
        evidence_relevance="Alignment reduces ambiguity, while the client remains the authority for the correct value.",
    ),
    "restore_verified_measurement": ClientDeliveryTemplate(
        decision_headline="Restore reliable measurement before acting on trends",
        business_impact="A required measurement source or comparison check is not currently reliable enough for a verified performance decision.",
        opportunity="Repair the confirmed measurement condition, collect fresh data, and pass readiness checks before evaluating trends.",
        action_title="Restore reliable connected measurement",
        why_now="Performance decisions should wait until the required source and comparison checks are reliable.",
        expected_result="Fresh connected data passes the required health, identity, comparison, and metric checks.",
        evidence_subject="Connected measurement check",
        evidence_observation="The saved measurement check found a source, identity, comparison, or metric condition that blocks reliable verification.",
        evidence_relevance="Restoring a trustworthy baseline prevents later decisions from being based on incomplete measurement.",
    ),
    "review_measurement_consistency": ClientDeliveryTemplate(
        decision_headline="Resolve the confirmed measurement disagreement",
        business_impact="Eligible connected sources do not currently point in the same direction for the saved comparison.",
        opportunity="Repeat the comparison on the same identity and time-window basis, then resolve or document the remaining difference.",
        action_title="Recheck measurement consistency",
        why_now="The saved source comparison needs a controlled recheck before a business cause is inferred.",
        expected_result="Fresh comparable data either resolves the direction difference or clearly documents the remaining limitation.",
        evidence_subject="Cross-source comparison",
        evidence_observation="The saved connected-source comparison recorded a direction difference on an eligible basis.",
        evidence_relevance="A controlled recheck separates a persistent measurement difference from a temporary or incomparable result.",
    ),
}


def get_client_delivery_template(template_key: str) -> ClientDeliveryTemplate:
    """Return a complete template or fail closed for an unsupported action type."""

    try:
        return CLIENT_DELIVERY_TEMPLATES[template_key]
    except KeyError as exc:
        raise ValueError(f"unsupported client delivery template: {template_key}") from exc
