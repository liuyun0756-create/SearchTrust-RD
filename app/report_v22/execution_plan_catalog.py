"""Versioned deterministic metric and presentation catalog for V22-074."""

from __future__ import annotations

from dataclasses import dataclass


METRIC_CATALOG_VERSION = "v22_execution_metrics_v1"
COPY_CATALOG_VERSION = "v22_verified_copy_v1"
RULESET_VERSION = "v22_execution_plan_v1"


@dataclass(frozen=True)
class PresentationTemplate:
    objective: str
    why_now: str
    explanation: str


PUBLIC_PRESENTATION = {
    "restore_site_access_indexing": PresentationTemplate(
        objective="Restore the intended availability and indexing state for the exact affected URLs.",
        why_now="The saved public evidence contains an explicit access or indexing condition that must be corrected before later optimization is evaluated.",
        explanation="Confirm the intended state, correct only the verified condition, and use a new bound site snapshot to verify completion.",
    ),
    "differentiate_page_titles": PresentationTemplate(
        objective="Give every targeted page a distinct, accurate title and verify the new saved HTML.",
        why_now="The selected public Finding identifies a repeated title across explicitly targeted pages.",
        explanation="Publish distinct titles that match each page purpose, then recheck the same final URLs without promising a ranking outcome.",
    ),
    "review_market_visibility": PresentationTemplate(
        objective="Repeat the confirmed market sample and document only responses supported by separate evidence.",
        why_now="The selected public Finding records a comparable visibility gap for the confirmed query set.",
        explanation="Preserve the query, location, device and result-type basis, then re-evaluate the saved market Finding without inferring a ranking cause.",
    ),
    "close_page_type_gap": PresentationTemplate(
        objective="Validate the sampled page-type difference and complete an approved build or no-build decision.",
        why_now="The selected public Finding records a page-type difference in the confirmed competitor sample.",
        explanation="Confirm the business need before creating a distinct useful page, or document why the sampled difference should not be copied.",
    ),
    "align_public_gbp": PresentationTemplate(
        objective="Align the exact website and public GBP fields using client-confirmed authoritative values.",
        why_now="The selected public Finding records a partial match, mismatch or required field missing across the website and public profile.",
        explanation="Correct only approved fields and verify exact alignment in new website and public GBP snapshots; public data is not official Performance.",
    ),
}


MEASUREMENT_PRESENTATION = {
    "restore_verified_measurement": PresentationTemplate(
        objective="Restore reliable GSC and GA4 measurement before evaluating later business actions.",
        why_now="A required source health, identity, comparison or metric gate currently blocks reliable verification.",
        explanation="Repair the saved measurement issue, collect fresh bound snapshots, and pass readiness checks before later actions are formally evaluated.",
    ),
    "review_measurement_consistency": PresentationTemplate(
        objective="Repeat the comparable source measurement and resolve or document the saved direction conflict.",
        why_now="Two eligible sources produced a saved direction-consistency Finding that needs a controlled recheck.",
        explanation="Recheck the same identity and time-window basis without assuming either source is wrong or attributing a business cause.",
    ),
}


MEASUREMENT_OWNERS = {
    "restore_verified_measurement": "Analytics implementation owner",
    "review_measurement_consistency": "Analytics and SEO leads",
}


MEASUREMENT_DONE = {
    "restore_verified_measurement": (
        "Fresh bound GSC and GA4 snapshots pass the required health, identity, comparison and metric checks.",
    ),
    "review_measurement_consistency": (
        "Fresh comparable snapshots no longer trigger the saved direction conflict, or the remaining comparison limitation is documented.",
    ),
}
