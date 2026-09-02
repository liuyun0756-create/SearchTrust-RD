"""Versioned deterministic templates for v2.2 public action skeletons."""

from __future__ import annotations

from dataclasses import dataclass

from app.report_v22.models import EffortBucket, SourceType
from app.report_v22.public_rule_catalog import (
    AHEAD,
    ASSET,
    DOMAIN,
    GBP_ALIGNMENT_RULE_VERSION,
    GBP_RULES,
    HTTP,
    NOINDEX,
    RULE_VERSION,
    TITLE,
)


CATALOG_VERSION = "v22_public_actions_v1"
TEMPLATE_VERSION = "1.0.0"


@dataclass(frozen=True)
class RuleActionBinding:
    rule_version: str
    template_key: str
    gbp_field: str | None = None


@dataclass(frozen=True)
class ActionTemplate:
    key: str
    version: str
    order: int
    id_slug: str
    steps: tuple[tuple[str, str], ...]
    content_requirements: tuple[str, ...]
    gbp_requirements: tuple[str, ...]
    technical_requirements: tuple[str, ...]
    required_client_assets: tuple[str, ...]
    owner_suggestion: str
    effort_bucket: EffortBucket
    definition_of_done: tuple[str, ...]
    metric_key: str
    metric_baseline: str
    metric_success_condition: str
    allowed_source_types: tuple[SourceType, ...]
    copy_allowed_fact_fields: tuple[str, ...]
    copy_required_limitations: tuple[str, ...]


ACTION_TEMPLATES = {
    "restore_site_access_indexing": ActionTemplate(
        key="restore_site_access_indexing",
        version=TEMPLATE_VERSION,
        order=10,
        id_slug="restore_site",
        steps=(
            ("Confirm the intended state", "Review each bound page finding and confirm the intended response and indexing policy."),
            ("Correct the confirmed condition", "Correct only the server response or explicit indexing directive confirmed as unintended."),
            ("Collect a new snapshot", "Recheck every targeted URL and retain new bound evidence."),
        ),
        content_requirements=(),
        gbp_requirements=(),
        technical_requirements=("Preserve redirects, canonicals and indexing directives that the client confirms are intentional.",),
        required_client_assets=("Client confirmation of the intended availability and indexing policy for each targeted URL.",),
        owner_suggestion="Technical SEO lead and web developer",
        effort_bucket="medium",
        definition_of_done=("Every targeted URL has a new eligible snapshot that no longer triggers the selected HTTP or explicit noindex finding.",),
        metric_key="site_access_indexing_recheck",
        metric_baseline="At least one targeted URL has a saved HTTP or explicit noindex finding.",
        metric_success_condition="New bound snapshots no longer trigger the selected condition.",
        allowed_source_types=("site",),
        copy_allowed_fact_fields=("finding.statement", "finding.scope", "finding.severity", "target.url", "action.definition_of_done"),
        copy_required_limitations=("Do not describe a saved page condition as permanent or promise a search outcome.",),
    ),
    "differentiate_page_titles": ActionTemplate(
        key="differentiate_page_titles",
        version=TEMPLATE_VERSION,
        order=20,
        id_slug="differentiate_titles",
        steps=(
            ("Review the title groups", "Confirm the purpose of each targeted URL and the repeated saved titles."),
            ("Write distinct titles", "Prepare a distinct accurate title for each page without changing the page offer."),
            ("Publish and recheck", "Publish approved titles and compare new bound page snapshots."),
        ),
        content_requirements=("Each targeted page title must accurately distinguish that page from the other targeted pages.",),
        gbp_requirements=(),
        technical_requirements=("Keep each approved title in the page title element and verify it in a new saved HTML snapshot.",),
        required_client_assets=("Client confirmation of the distinct purpose of each targeted page.",),
        owner_suggestion="SEO content lead",
        effort_bucket="small",
        definition_of_done=("New eligible snapshots show distinct approved titles across the targeted final URLs.",),
        metric_key="duplicate_title_recheck",
        metric_baseline="The saved sample contains a repeated normalized title across targeted final URLs.",
        metric_success_condition="New bound snapshots no longer trigger the selected duplicate-title finding.",
        allowed_source_types=("site",),
        copy_allowed_fact_fields=("finding.statement", "finding.scope", "target.url", "action.definition_of_done"),
        copy_required_limitations=("Do not claim that repeated titles prove duplicate body content.",),
    ),
    "review_market_visibility": ActionTemplate(
        key="review_market_visibility",
        version=TEMPLATE_VERSION,
        order=30,
        id_slug="review_market",
        steps=(
            ("Confirm comparable observations", "Review the selected queries, locations, devices and result types represented by the bound findings."),
            ("Document supported responses", "Identify only changes supported by separate site or profile evidence; record unresolved causes as unknown."),
            ("Repeat the market sample", "Collect fresh comparable search observations and re-evaluate the selected findings."),
        ),
        content_requirements=(),
        gbp_requirements=(),
        technical_requirements=("Preserve the same query, location, device and result-type basis when validating the market observation.",),
        required_client_assets=("Client confirmation of the priority queries represented by the selected findings.",),
        owner_suggestion="Local SEO lead",
        effort_bucket="medium",
        definition_of_done=("Fresh comparable market evidence is captured and every selected market finding is re-evaluated without an unsupported causal claim.",),
        metric_key="market_visibility_recheck",
        metric_baseline="The client domain was unobserved or behind confirmed competitors in at least one saved comparable result.",
        metric_success_condition="Fresh comparable observations are stored and the selected conditions are re-evaluated.",
        allowed_source_types=("serp",),
        copy_allowed_fact_fields=("finding.statement", "finding.scope", "target.query", "target.site", "action.definition_of_done"),
        copy_required_limitations=("Do not infer a ranking cause or promise visibility improvement from the saved observations.",),
    ),
    "close_page_type_gap": ActionTemplate(
        key="close_page_type_gap",
        version=TEMPLATE_VERSION,
        order=40,
        id_slug="page_type_gap",
        steps=(
            ("Validate the sampled gap", "Confirm the page classifications and whether the targeted page type represents a real client need."),
            ("Approve the page requirement", "Define an accurate page brief only when the client confirms the underlying offering or business need."),
            ("Create or document the decision", "Publish an approved distinct page or record why the sampled difference should not be copied."),
        ),
        content_requirements=("Use only verified offerings, locations, proof and customer needs relevant to the targeted page type.",),
        gbp_requirements=(),
        technical_requirements=("If a page is approved, make it independently useful, reachable and eligible for a new inventory snapshot.",),
        required_client_assets=("Client confirmation that the targeted page type represents a real offering or business need.", "Approved supporting proof for any new page."),
        owner_suggestion="SEO content lead and client subject-matter expert",
        effort_bucket="medium",
        definition_of_done=("The sampled page-type difference is validated and an approved page or documented no-build decision is available for recheck.",),
        metric_key="page_type_gap_recheck",
        metric_baseline="The client sample lacked the targeted page type while multiple confirmed competitor samples contained it.",
        metric_success_condition="New bound inventories support a documented decision for the targeted page type.",
        allowed_source_types=("site", "competitor"),
        copy_allowed_fact_fields=("finding.statement", "finding.scope", "target.page_type", "action.definition_of_done"),
        copy_required_limitations=("Describe this as a sampled page-type difference, not proof that the client must copy a competitor asset.",),
    ),
    "align_public_gbp": ActionTemplate(
        key="align_public_gbp",
        version=TEMPLATE_VERSION,
        order=50,
        id_slug="align_public_gbp",
        steps=(
            ("Confirm authoritative values", "Obtain client confirmation for every targeted website and public GBP field."),
            ("Correct the approved channels", "Update only the eligible website or public GBP fields the client confirms are inaccurate or missing."),
            ("Collect new bound snapshots", "Recheck the targeted fields on both channels and retain their source state."),
        ),
        content_requirements=("Use only client-approved business identity and field values.",),
        gbp_requirements=("Change a public GBP field only after the client confirms the authoritative value and channel access.",),
        technical_requirements=(),
        required_client_assets=("Client-confirmed authoritative values for every targeted GBP field.", "Access to each channel that requires an approved correction."),
        owner_suggestion="Client GBP manager and website owner",
        effort_bucket="small",
        definition_of_done=("New eligible website and public GBP snapshots no longer trigger the selected field-alignment findings.",),
        metric_key="site_gbp_alignment_recheck",
        metric_baseline="At least one targeted website and public GBP field was partially matched, mismatched or missing.",
        metric_success_condition="New bound snapshots no longer trigger the selected field-alignment findings.",
        allowed_source_types=("site", "gbp", "coverage"),
        copy_allowed_fact_fields=("finding.statement", "finding.scope", "target.url", "target.gbp_field", "action.definition_of_done"),
        copy_required_limitations=("Do not invent a missing field value or imply access to an unauthorized GBP account.",),
    ),
}


RULE_ACTIONS = {
    HTTP: RuleActionBinding(RULE_VERSION, "restore_site_access_indexing"),
    NOINDEX: RuleActionBinding(RULE_VERSION, "restore_site_access_indexing"),
    TITLE: RuleActionBinding(RULE_VERSION, "differentiate_page_titles"),
    DOMAIN: RuleActionBinding(RULE_VERSION, "review_market_visibility"),
    AHEAD: RuleActionBinding(RULE_VERSION, "review_market_visibility"),
    ASSET: RuleActionBinding(RULE_VERSION, "close_page_type_gap"),
    GBP_RULES[0]: RuleActionBinding(GBP_ALIGNMENT_RULE_VERSION, "align_public_gbp", "business_name"),
    GBP_RULES[1]: RuleActionBinding(GBP_ALIGNMENT_RULE_VERSION, "align_public_gbp", "address"),
    GBP_RULES[2]: RuleActionBinding(GBP_ALIGNMENT_RULE_VERSION, "align_public_gbp", "phone"),
    GBP_RULES[3]: RuleActionBinding(GBP_ALIGNMENT_RULE_VERSION, "align_public_gbp", "service_area"),
}
