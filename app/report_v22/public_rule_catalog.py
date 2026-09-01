"""Versioned product audit rules, not search-engine penalty thresholds."""
from dataclasses import dataclass

from app.report_v22.models import Confidence, FactClassification, Severity

RULESET_VERSION = "v22_public_findings_v1"
RULE_VERSION = "1.0.0"
GBP_ALIGNMENT_RULE_VERSION = "1.1.0"
HTTP = "v22_public.site_http_error"
NOINDEX = "v22_public.site_explicit_noindex"
TITLE = "v22_public.site_duplicate_title"
DOMAIN = "v22_public.market_site_domain_unobserved"
AHEAD = "v22_public.market_confirmed_competitors_ahead"
ASSET = "v22_public.competitor_sample_page_type_gap"
GBP_RULES = tuple(f"v22_public.gbp_{field}_alignment" for field in ("name", "address", "phone", "service_area"))


@dataclass(frozen=True)
class RuleSpec:
    classification: FactClassification
    severity: Severity
    confidence: Confidence
    change_condition: str


RULES = {
    HTTP: RuleSpec("fact", "medium", "medium", "Obtain a new valid response snapshot for the affected URL and check its HTTP status again."),
    NOINDEX: RuleSpec("fact", "medium", "medium", "Confirm the intended indexing policy and inspect a new page snapshot for its explicit meta directives."),
    TITLE: RuleSpec("fact", "low", "medium", "Inspect new snapshots of the affected final URLs and compare their observed titles again."),
    DOMAIN: RuleSpec("fact", "low", "low", "Repeat the confirmed query in the same location, device and result type, with URL-bearing results."),
    AHEAD: RuleSpec("fact", "medium", "medium", "Obtain new comparable positions for the client and confirmed competitor domains in the same search context."),
    ASSET: RuleSpec("inference", "low", "low", "Obtain contemporaneous inventories, verify page classifications and check whether the client sample includes this page type."),
}

GBP_RULE_SPECS = {
    GBP_RULES[0]: RuleSpec("fact", "medium", "low", "Update the public business name or the eligible website business name, then inspect new bound snapshots."),
    GBP_RULES[1]: RuleSpec("fact", "high", "low", "Update the public business address or the eligible website address, then inspect new bound snapshots."),
    GBP_RULES[2]: RuleSpec("fact", "medium", "low", "Update the public business phone or the eligible website phone, then inspect new bound snapshots."),
    GBP_RULES[3]: RuleSpec("fact", "medium", "low", "Update the public service areas or the eligible website service areas, then inspect new bound snapshots."),
}
