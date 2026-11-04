"""P0-06 factual observation states; availability never implies absence."""

GBP_STATUSES = {
    "observed": "confirmed",
    "returned_empty": "not_detected",
    "not_returned": "not_checked",
    "unsupported": "not_checked",
    "request_failed": "not_checked",
}
ALIGNMENT_STATUSES = {
    "exact_match": "confirmed",
    "semantic_match": "confirmed",
    "partial_match": "confirmed",  # Only the compared parts; must carry scope.
    "mismatch": "confirmed",      # Only an eligible, actually compared pair.
    "site_missing": "not_detected",
    "not_checked": "not_checked",
    "not_applicable": "not_assessed",
}


def gbp_status(state: str) -> str:
    """A missing/unsupported provider field is not a confirmed empty value."""
    return GBP_STATUSES[state]


def alignment_status(state: str, *, gbp_field_state: str | None = None) -> str:
    """Legacy missing states require the original provider's field state."""
    if state in {"gbp_missing", "both_missing"}:
        # The alignment enum alone cannot distinguish absence from failure.
        return "not_detected" if gbp_field_state == "returned_empty" else "not_checked"
    return ALIGNMENT_STATUSES[state]

# New rulesets only. Historical generation continues to use its frozen branch.
PRIOR_PUBLIC_RULESET = "v22_capability_public_v8"
RESOURCE_RULESET = "v22_capability_public_v9"
PUBLIC_RULESET = "v22_capability_public_v10"
RESOURCE_RULESETS = {RESOURCE_RULESET, PUBLIC_RULESET}
PUBLIC_LABELLED_RULESETS = {PRIOR_PUBLIC_RULESET, *RESOURCE_RULESETS}
SERVICE_RULESET = "v22_capability_service_v5"
LABELLED_RULESETS = {*PUBLIC_LABELLED_RULESETS, SERVICE_RULESET}
COVERAGE_SOURCES = ("site", "gbp", "serp", "competitor", "citation", "gsc", "ga4", "rendered")


def rule_kinds():
    from app.report_v22.public_rule_catalog import RULES, GBP_RULE_SPECS
    from app.report_v22 import first_party_gsc_findings as gsc, first_party_ga4_findings as ga4, first_party_gbp_findings as gbp
    from app.report_v22.cross_source_findings import RULE_ORDER
    result = {key: "inference" if spec.classification != "fact" else "fact"
              for key, spec in {**RULES, **GBP_RULE_SPECS}.items()}
    result.update({key: "fact" for key in (
        "v22_capability.public_assessment", "v22_capability.service_source_check", "v22_capability.service_title_review",
        gsc.QUERY_OPPORTUNITY, gsc.PAGE_OPPORTUNITY, gsc.DIMENSION_DECLINE, gsc.DIMENSION_GROWTH,
        ga4.ENGAGEMENT_GAP, ga4.CONVERSION_GAP, ga4.PAGE_CHANGE,
        gbp.IMPRESSION_CHANGE, gbp.ACTION_CHANGE, gbp.SEARCH_DEMAND, *RULE_ORDER,
    )})
    for module in (gsc, ga4, gbp):
        result.update({f"{module.MEASUREMENT}.{reason}": "fact" for reason in module.MEASUREMENT_REASONS})
    return result


def _source(item):
    return "gbp" if item["source_kind"] in {"public_gbp", "official_gbp"} else item["source_kind"]


def _state_observation(item):
    paths = item["field_path"].split("\n")
    return any(p.endswith(("/state", "/status", "/collection_status")) for p in paths)


def annotate_findings(raw):
    """Persist source coverage for this finding, not whole-report availability."""
    kinds = rule_kinds()
    evidence = {e["id"]: e for e in raw["evidence"]}
    for finding in raw["findings"]:
        if finding.get("kind") is not None:
            continue
        kind = kinds[finding["rule_id"]]  # New rules must declare their mapping.
        items = [evidence[key] for key in finding["evidence_ids"]]
        states = [e["value"] for e in items if _state_observation(e) and isinstance(e["value"], str)]
        if kind != "fact" or finding["rule_id"] == "v22_capability.public_assessment":
            status = "not_assessed"
        elif any(state in {"not_returned", "unsupported", "request_failed", "not_checked", "failed"} for state in states):
            status = "not_checked"
        elif finding["rule_id"] == "v22_public.market_site_domain_unobserved" or any(state in {"returned_empty", "site_missing", "gbp_missing"} for state in states):
            status = "not_detected"
        elif ".PROFILE_OR_MEASUREMENT." in finding["rule_id"] and finding["rule_id"].endswith("_MISSING"):
            # Official profile normalization does not retain public five-state provenance.
            status = "not_checked"
        else:
            status = "confirmed"
        checked = {_source(e) for e in items if not (_state_observation(e)
            and isinstance(e["value"], str) and e["value"] in {"not_returned", "unsupported", "request_failed", "not_checked", "failed"})}
        finding.update(kind=kind, status=status,
            coverage=[{"source":source, "checked":source in checked} for source in COVERAGE_SOURCES])
