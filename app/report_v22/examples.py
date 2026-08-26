"""Deterministic source data for the shared v2.2 contract fixtures."""

from __future__ import annotations

from copy import deepcopy
from typing import Any


CASE_ID = "11111111-1111-4111-8111-111111111111"
PROSPECT_REPORT_ID = "22222222-2222-4222-8222-222222222222"
VERIFIED_REPORT_ID = "33333333-3333-4333-8333-333333333333"

SITE_SNAPSHOT_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
SERP_SNAPSHOT_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
PUBLIC_GBP_SNAPSHOT_ID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
GSC_SNAPSHOT_ID = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
GBP_SNAPSHOT_ID = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
GA4_SNAPSHOT_ID = "ffffffff-ffff-4fff-8fff-ffffffffffff"


def _target_market() -> dict[str, Any]:
    return {
        "display_name": "Austin, TX, US",
        "country_code": "US",
        "region": "TX",
        "city": "Austin",
        "postal_code": "78701",
        "latitude": 30.2672,
        "longitude": -97.7431,
    }


def _business() -> dict[str, Any]:
    return {
        "business_name": "Example Plumbing",
        "site_url": "https://example-plumbing.test",
        "normalized_domain": "example-plumbing.test",
        "operating_model": "service_area",
        "primary_location": _target_market(),
        "public_gbp_url": "https://maps.google.com/?cid=123456789",
    }


def _source_locator(**overrides: Any) -> dict[str, Any]:
    locator = {
        "url": None,
        "external_resource_id": None,
        "field_path": None,
        "page_path": None,
        "query": None,
        "latitude": None,
        "longitude": None,
        "device": None,
        "language": None,
    }
    locator.update(overrides)
    return locator


def _evidence(
    evidence_id: str,
    snapshot_id: str,
    source_type: str,
    original_value: str | int | float | bool | None,
    normalized_value: str | int | float | bool | None,
    *,
    locator: dict[str, Any],
    health_status: str = "healthy",
    coverage_start: str | None = None,
    coverage_end: str | None = None,
) -> dict[str, Any]:
    return {
        "evidence_id": evidence_id,
        "snapshot_id": snapshot_id,
        "source_type": source_type,
        "source_locator": locator,
        "original_value": original_value,
        "normalized_value": normalized_value,
        "collected_at": "2026-08-26T08:00:00Z",
        "coverage_start": coverage_start,
        "coverage_end": coverage_end,
        "confidence": "high",
        "health_status": health_status,
        "limitations": [],
    }


def _coverage_source(
    source_type: str,
    health_status: str,
    identity_match_status: str,
    snapshot_ids: list[str],
    summary: str,
) -> dict[str, Any]:
    return {
        "source_type": source_type,
        "health_status": health_status,
        "identity_match_status": identity_match_status,
        "snapshot_ids": snapshot_ids,
        "checked_items": 1 if snapshot_ids else 0,
        "available_items": 1 if health_status == "healthy" else 0,
        "coverage_summary": summary,
        "limitations": [],
    }


def _not_connected_source(source_type: str) -> dict[str, Any]:
    return {
        "source_type": source_type,
        "connection_state": "not_connected",
        "snapshot_id": None,
        "identity_match_status": "not_checked",
        "health_status": "not_checked",
        "coverage_start": None,
        "coverage_end": None,
        "metrics": [],
        "health_reasons": ["Client data has not been connected."],
        "limitations": ["This prospect report uses public data only."],
    }


def _layer(layer_key: str, finding_ids: list[str], evidence_ids: list[str]) -> dict[str, Any]:
    return {
        "layer_key": layer_key,
        "status": "weak" if finding_ids else "not_checked",
        "summary": "This layer contains a verified opportunity." if finding_ids else "No decisive issue was selected for this layer.",
        "finding_ids": finding_ids,
        "evidence_ids": evidence_ids,
    }


def build_prospect_fixture() -> dict[str, Any]:
    evidence = [
        _evidence(
            "ev_site_service_page",
            SITE_SNAPSHOT_ID,
            "site",
            "No dedicated emergency plumbing page",
            False,
            locator=_source_locator(url="https://example-plumbing.test/services", field_path="page_inventory.emergency_plumbing"),
        ),
        _evidence(
            "ev_serp_competitor_alpha",
            SERP_SNAPSHOT_ID,
            "serp",
            1,
            1,
            locator=_source_locator(query="emergency plumber austin", latitude=30.2672, longitude=-97.7431, device="mobile", language="en"),
        ),
        _evidence(
            "ev_serp_competitor_beta",
            SERP_SNAPSHOT_ID,
            "serp",
            2,
            2,
            locator=_source_locator(query="24 hour plumber austin", latitude=30.2672, longitude=-97.7431, device="mobile", language="en"),
        ),
        _evidence(
            "ev_serp_competitor_gamma",
            SERP_SNAPSHOT_ID,
            "serp",
            3,
            3,
            locator=_source_locator(query="emergency plumbing austin", latitude=30.2672, longitude=-97.7431, device="mobile", language="en"),
        ),
        _evidence(
            "ev_public_gbp_service_gap",
            PUBLIC_GBP_SNAPSHOT_ID,
            "gbp",
            "Plumber",
            "Emergency plumbing service not listed",
            locator=_source_locator(url="https://maps.google.com/?cid=123456789", field_path="services"),
        ),
    ]

    findings = [
        {
            "finding_id": "fn_market_visibility_gap",
            "statement": "Three relevant competitors appear ahead of the business for the confirmed local queries.",
            "evidence_ids": ["ev_serp_competitor_alpha", "ev_serp_competitor_beta", "ev_serp_competitor_gamma"],
            "comparator_ids": [],
            "rule_id": "V22.PUBLIC.MARKET_GAP",
            "rule_version": "1.0.0",
            "classification": "fact",
            "severity": "high",
            "scope": "Confirmed Austin emergency plumbing queries",
            "confidence": "high",
            "affected_urls": ["https://example-plumbing.test"],
            "affected_queries": ["emergency plumber austin", "24 hour plumber austin", "emergency plumbing austin"],
            "missing_data": [],
            "change_conditions": ["New comparable SERP observations show the business consistently outranking the selected competitors."],
        },
        {
            "finding_id": "fn_service_page_gap",
            "statement": "The site lacks a dedicated page for the primary emergency plumbing opportunity.",
            "evidence_ids": ["ev_site_service_page"],
            "comparator_ids": [],
            "rule_id": "V22.PUBLIC.SERVICE_PAGE_GAP",
            "rule_version": "1.0.0",
            "classification": "fact",
            "severity": "high",
            "scope": "Service page inventory",
            "confidence": "high",
            "affected_urls": ["https://example-plumbing.test/services"],
            "affected_queries": ["emergency plumber austin"],
            "missing_data": [],
            "change_conditions": ["A crawl discovers a dedicated, indexable emergency plumbing page."],
        },
        {
            "finding_id": "fn_public_gbp_service_gap",
            "statement": "The public GBP service set does not explicitly describe the primary emergency plumbing service.",
            "evidence_ids": ["ev_public_gbp_service_gap"],
            "comparator_ids": [],
            "rule_id": "V22.PUBLIC.GBP_SERVICE_GAP",
            "rule_version": "1.0.0",
            "classification": "fact",
            "severity": "medium",
            "scope": "Public GBP service information",
            "confidence": "high",
            "affected_urls": ["https://example-plumbing.test"],
            "affected_queries": ["emergency plumbing austin"],
            "missing_data": ["Official GBP Performance is unavailable before client authorization."],
            "change_conditions": ["A fresh public GBP snapshot lists an aligned emergency plumbing service."],
        },
    ]

    actions = [
        {
            "action_id": "ac_publish_emergency_page",
            "sequence": 1,
            "finding_ids": ["fn_service_page_gap", "fn_market_visibility_gap"],
            "why_now": "The missing service asset blocks relevance for the confirmed opportunity.",
            "exact_targets": ["https://example-plumbing.test/services/emergency-plumbing"],
            "implementation_steps": [
                {"sequence": 1, "title": "Create the service page", "instruction": "Publish an indexable emergency plumbing page for Austin."},
                {"sequence": 2, "title": "Connect the page", "instruction": "Link the page from services and relevant location navigation."},
            ],
            "specification": {
                "content_requirements": ["Describe emergency response scope, proof, service area, and contact path."],
                "gbp_requirements": [],
                "technical_requirements": ["Use a self-referencing canonical and include the page in the sitemap."],
            },
            "required_client_assets": ["Emergency service availability", "Service photos", "Response-time policy"],
            "dependencies": [],
            "owner_suggestion": "SEO lead and client subject-matter expert",
            "effort_bucket": "medium",
            "definition_of_done": ["The page is live, indexable, internally linked, and quality checked."],
            "validation_metrics": [{"metric_key": "page_indexable", "baseline": "No dedicated page", "success_condition": "Dedicated page is indexable", "source_type": "site"}],
            "data_sources": ["site", "serp"],
            "review_date": "2026-09-25",
            "client_facing_explanation": "Create one focused page that gives searchers and Google a clear emergency plumbing destination.",
        },
        {
            "action_id": "ac_align_public_gbp",
            "sequence": 2,
            "finding_ids": ["fn_public_gbp_service_gap"],
            "why_now": "The public profile should describe the same primary service as the website.",
            "exact_targets": ["Google Business Profile service information"],
            "implementation_steps": [{"sequence": 1, "title": "Confirm service eligibility", "instruction": "Verify that emergency plumbing is an accurate offered service before proposing the profile update."}],
            "specification": {
                "content_requirements": [],
                "gbp_requirements": ["Add an accurate emergency plumbing service description after client approval."],
                "technical_requirements": [],
            },
            "required_client_assets": ["Approved service description"],
            "dependencies": ["ac_publish_emergency_page"],
            "owner_suggestion": "Client GBP manager",
            "effort_bucket": "small",
            "definition_of_done": ["The approved service is represented consistently on the site and GBP."],
            "validation_metrics": [{"metric_key": "gbp_service_alignment", "baseline": "Primary service missing", "success_condition": "Primary service is accurately represented", "source_type": "gbp"}],
            "data_sources": ["gbp", "site"],
            "review_date": "2026-10-25",
            "client_facing_explanation": "Align the public profile with the service customers are actively looking for.",
        },
        {
            "action_id": "ac_build_local_proof",
            "sequence": 3,
            "finding_ids": ["fn_market_visibility_gap"],
            "why_now": "Competitor visibility creates a measurable need for stronger local proof after the core page exists.",
            "exact_targets": ["Emergency plumbing service page", "Relevant Austin proof assets"],
            "implementation_steps": [{"sequence": 1, "title": "Add local proof", "instruction": "Add verified Austin service examples and customer proof to the new page."}],
            "specification": {
                "content_requirements": ["Use only verifiable service examples and customer-approved proof."],
                "gbp_requirements": [],
                "technical_requirements": [],
            },
            "required_client_assets": ["Approved Austin job examples", "Customer-approved proof"],
            "dependencies": ["ac_publish_emergency_page", "ac_align_public_gbp"],
            "owner_suggestion": "Content lead",
            "effort_bucket": "medium",
            "definition_of_done": ["The service page contains approved, locally specific proof."],
            "validation_metrics": [{"metric_key": "local_proof_items", "baseline": "No dedicated proof set", "success_condition": "At least three approved proof items are published", "source_type": "site"}],
            "data_sources": ["site", "competitor"],
            "review_date": "2026-11-24",
            "client_facing_explanation": "Support the new service page with real local proof that competitors already communicate more clearly.",
        },
    ]

    return {
        "identity": {"case_id": CASE_ID, "business": _business()},
        "case_context": {
            "primary_service": "Emergency plumbing",
            "target_market": _target_market(),
            "queries": ["emergency plumber austin", "24 hour plumber austin", "emergency plumbing austin"],
            "search_language": "en",
            "search_device": "mobile",
        },
        "report_version": {
            "schema_version": "2.2.0",
            "report_id": PROSPECT_REPORT_ID,
            "report_type": "prospect",
            "version_number": 1,
            "parent_report_id": None,
            "generated_at": "2026-08-26T08:30:00Z",
            "ruleset_version": "v22-public-1.0.0",
            "copy_model_version": "dify-copy-v22-1",
        },
        "data_coverage": {
            "full_evidence_coverage": False,
            "sources": [
                _coverage_source("site", "healthy", "matched", [SITE_SNAPSHOT_ID], "Twelve URLs checked; one deeply analyzed."),
                _coverage_source("serp", "healthy", "matched", [SERP_SNAPSHOT_ID], "Three queries checked at one confirmed target point."),
                _coverage_source("competitor", "healthy", "matched", [SERP_SNAPSHOT_ID], "Three competitors selected in comparable search contexts."),
                _coverage_source("gbp", "healthy", "needs_confirmation", [PUBLIC_GBP_SNAPSHOT_ID], "Public GBP data checked; official performance is not connected."),
                _coverage_source("gsc", "not_checked", "not_checked", [], "GSC is not connected for the prospect report."),
                _coverage_source("ga4", "not_checked", "not_checked", [], "GA4 is not connected for the prospect report."),
            ],
            "limitations": ["The prospect report does not use authorized GSC, GBP Performance, or GA4 data."],
        },
        "market_snapshot": {
            "observed_at": "2026-08-26T08:00:00Z",
            "target_market": _target_market(),
            "queries": ["emergency plumber austin", "24 hour plumber austin", "emergency plumbing austin"],
            "results": [
                {"query": "emergency plumber austin", "result_type": "local_pack", "position": 1, "business_name": "Alpha Plumbing", "url": "https://alpha-plumbing.test", "evidence_id": "ev_serp_competitor_alpha"},
                {"query": "24 hour plumber austin", "result_type": "maps", "position": 2, "business_name": "Beta Plumbing", "url": "https://beta-plumbing.test", "evidence_id": "ev_serp_competitor_beta"},
                {"query": "emergency plumbing austin", "result_type": "organic", "position": 3, "business_name": "Gamma Plumbing", "url": "https://gamma-plumbing.test", "evidence_id": "ev_serp_competitor_gamma"},
            ],
            "summary": "Three relevant competitors own the strongest observed positions across the confirmed query set.",
            "limitations": ["One primary target point and mobile context were used."],
        },
        "site_inventory_summary": {
            "discovered_url_count": 12,
            "structurally_checked_count": 12,
            "deep_analyzed_count": 1,
            "discovery_limit": 500,
            "deep_analysis_limit": 50,
            "page_type_counts": [{"label": "service", "count": 4}, {"label": "other", "count": 8}],
            "selected_pages": [{"url": "https://example-plumbing.test/services", "page_type": "service_index", "crawl_depth": 1, "deep_analyzed": True, "evidence_ids": ["ev_site_service_page"]}],
            "limitations": [],
        },
        "competitor_analysis": {
            "selection_method": "system_ranked_user_confirmed",
            "competitors": [
                {"competitor_id": "cp_alpha_plumbing", "business_name": "Alpha Plumbing", "website_url": "https://alpha-plumbing.test", "public_gbp_url": None, "query_appearance_count": 3, "best_position": 1, "analyzed_page_count": 3, "strengths": ["Dedicated emergency service page"], "gaps": [], "evidence_ids": ["ev_serp_competitor_alpha"]},
                {"competitor_id": "cp_beta_plumbing", "business_name": "Beta Plumbing", "website_url": "https://beta-plumbing.test", "public_gbp_url": None, "query_appearance_count": 2, "best_position": 2, "analyzed_page_count": 2, "strengths": ["Clear local proof"], "gaps": [], "evidence_ids": ["ev_serp_competitor_beta"]},
                {"competitor_id": "cp_gamma_plumbing", "business_name": "Gamma Plumbing", "website_url": "https://gamma-plumbing.test", "public_gbp_url": None, "query_appearance_count": 2, "best_position": 3, "analyzed_page_count": 2, "strengths": ["Aligned service information"], "gaps": [], "evidence_ids": ["ev_serp_competitor_gamma"]},
            ],
            "comparison_summary": "All three selected competitors present stronger emergency plumbing relevance in comparable contexts.",
            "limitations": ["Competitor analysis is limited to public data and ten pages per competitor."],
        },
        "first_party_performance": {
            "gsc": _not_connected_source("gsc"),
            "gbp": _not_connected_source("gbp"),
            "ga4": _not_connected_source("ga4"),
        },
        "executive_decision": {
            "core_problem": "The business lacks a dedicated, locally proven emergency plumbing asset while three competitors own the observed search positions.",
            "finding_ids": ["fn_service_page_gap", "fn_market_visibility_gap"],
            "why_now": "The gap is visible in both site inventory and comparable local search results.",
            "decision_summary": "Build the missing asset first, align the public profile second, then strengthen local proof.",
        },
        "eight_layers": [
            _layer("foundation", [], []),
            _layer("entity_presence", ["fn_public_gbp_service_gap"], ["ev_public_gbp_service_gap"]),
            _layer("entity_consistency", [], []),
            _layer("specificity", ["fn_service_page_gap"], ["ev_site_service_page"]),
            _layer("real_world_connection", ["fn_market_visibility_gap"], ["ev_serp_competitor_alpha"]),
            _layer("accountability", [], []),
            _layer("page_unique_value", ["fn_service_page_gap"], ["ev_site_service_page"]),
            _layer("algorithm_fit", ["fn_market_visibility_gap"], ["ev_serp_competitor_beta", "ev_serp_competitor_gamma"]),
        ],
        "findings": findings,
        "top_actions": actions,
        "roadmap_30_60_90": {
            "phases": [
                {"period": "days_1_30", "objective": "Create the missing search destination.", "action_ids": ["ac_publish_emergency_page"], "exit_criteria": ["The page is live and indexable."]},
                {"period": "days_31_60", "objective": "Align the public business profile.", "action_ids": ["ac_align_public_gbp"], "exit_criteria": ["Service information is consistent."]},
                {"period": "days_61_90", "objective": "Add locally specific proof.", "action_ids": ["ac_build_local_proof"], "exit_criteria": ["Approved local proof is published."]},
            ]
        },
        "client_summary": {
            "headline": "Create one clear emergency plumbing path before expanding the campaign.",
            "core_problem": "The current site and public profile do not present a complete emergency plumbing destination.",
            "opportunity": "A focused asset can address a visible gap against three confirmed competitors.",
            "action_ids": ["ac_publish_emergency_page", "ac_align_public_gbp", "ac_build_local_proof"],
            "required_client_assets": ["Service details", "Approved GBP description", "Local job proof"],
            "next_review_date": "2026-11-24",
        },
        "evidence_index": evidence,
        "version_diff": {"kind": "initial", "parent_report_id": None, "entries": []},
        "limitations": [
            {"limitation_id": "lim_public_only", "category": "coverage", "severity": "medium", "description": "No authorized Google first-party data is used in this prospect report.", "affected_sections": ["first_party_performance", "data_coverage"]}
        ],
    }


def _connected_source(source_type: str, snapshot_id: str, metrics: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "source_type": source_type,
        "connection_state": "verified",
        "snapshot_id": snapshot_id,
        "identity_match_status": "matched",
        "health_status": "healthy",
        "coverage_start": "2026-05-28",
        "coverage_end": "2026-08-25",
        "metrics": metrics,
        "health_reasons": [],
        "limitations": [],
    }


def build_verified_fixture() -> dict[str, Any]:
    report = deepcopy(build_prospect_fixture())
    report["report_version"] = {
        "schema_version": "2.2.0",
        "report_id": VERIFIED_REPORT_ID,
        "report_type": "verified_execution",
        "version_number": 2,
        "parent_report_id": PROSPECT_REPORT_ID,
        "generated_at": "2026-08-26T09:30:00Z",
        "ruleset_version": "v22-verified-1.0.0",
        "copy_model_version": "dify-copy-v22-1",
    }
    report["data_coverage"] = {
        "full_evidence_coverage": True,
        "sources": [
            _coverage_source("site", "healthy", "matched", [SITE_SNAPSHOT_ID], "Twelve URLs checked; one deeply analyzed."),
            _coverage_source("serp", "healthy", "matched", [SERP_SNAPSHOT_ID], "Three queries checked at one confirmed target point."),
            _coverage_source("competitor", "healthy", "matched", [SERP_SNAPSHOT_ID], "Three competitors selected in comparable contexts."),
            _coverage_source("gsc", "healthy", "matched", [GSC_SNAPSHOT_ID], "Current and previous 90-day Search Console periods are available."),
            _coverage_source("gbp", "healthy", "matched", [GBP_SNAPSHOT_ID], "The managed Location and Performance period are available."),
            _coverage_source("ga4", "healthy", "matched", [GA4_SNAPSHOT_ID], "Landing page and key-event data are available."),
        ],
        "limitations": ["Search Console query data remains subject to provider privacy filtering."],
    }
    report["first_party_performance"] = {
        "gsc": _connected_source("gsc", GSC_SNAPSHOT_ID, [{"metric_key": "impressions", "label": "Emergency query impressions", "value": 4200.0, "unit": "count", "comparison_value": 3500.0}]),
        "gbp": _connected_source("gbp", GBP_SNAPSHOT_ID, [{"metric_key": "website_clicks", "label": "GBP website clicks", "value": 84.0, "unit": "count", "comparison_value": 79.0}]),
        "ga4": _connected_source("ga4", GA4_SNAPSHOT_ID, [{"metric_key": "engagement_rate", "label": "Service landing page engagement rate", "value": 0.31, "unit": "ratio", "comparison_value": 0.37}]),
    }
    report["evidence_index"].extend(
        [
            _evidence("ev_gsc_low_ctr", GSC_SNAPSHOT_ID, "gsc", 0.012, 0.012, locator=_source_locator(external_resource_id="sc-domain:example-plumbing.test", query="emergency plumber austin", field_path="ctr"), coverage_start="2026-05-28", coverage_end="2026-08-25"),
            _evidence("ev_gbp_website_clicks", GBP_SNAPSHOT_ID, "gbp", 84, 84, locator=_source_locator(external_resource_id="locations/123456", field_path="performance.website_clicks"), coverage_start="2026-05-28", coverage_end="2026-08-25"),
            _evidence("ev_ga4_low_engagement", GA4_SNAPSHOT_ID, "ga4", 0.31, 0.31, locator=_source_locator(external_resource_id="properties/987654", page_path="/services", field_path="engagement_rate"), coverage_start="2026-05-28", coverage_end="2026-08-25"),
        ]
    )
    report["findings"][0]["statement"] = "Search Console confirms meaningful emergency-query impressions while comparable competitors retain stronger observed positions."
    report["findings"][0]["evidence_ids"].append("ev_gsc_low_ctr")
    report["findings"][1]["statement"] = "The missing dedicated page is confirmed as the first priority, and GA4 shows the existing service index has weak engagement."
    report["findings"][1]["evidence_ids"].append("ev_ga4_low_engagement")
    report["findings"][2]["statement"] = "GBP Performance confirms customer activity, but the primary emergency service remains absent from the aligned service information."
    report["findings"][2]["evidence_ids"].append("ev_gbp_website_clicks")
    report["top_actions"][0]["data_sources"] = ["site", "serp", "gsc", "ga4"]
    report["top_actions"][0]["validation_metrics"].append({"metric_key": "gsc_emergency_ctr", "baseline": "1.2% CTR", "success_condition": "CTR improves without losing qualified impressions", "source_type": "gsc"})
    report["top_actions"][1]["data_sources"] = ["gbp", "site"]
    report["top_actions"][1]["validation_metrics"].append({"metric_key": "gbp_website_clicks", "baseline": "84 clicks in 90 days", "success_condition": "Qualified website actions increase after alignment", "source_type": "gbp"})
    report["top_actions"][2]["data_sources"] = ["site", "competitor", "ga4"]
    report["top_actions"][2]["validation_metrics"].append({"metric_key": "ga4_engagement_rate", "baseline": "31% engagement rate", "success_condition": "Engagement rate improves on the target landing path", "source_type": "ga4"})
    report["version_diff"] = {
        "kind": "upgrade",
        "parent_report_id": PROSPECT_REPORT_ID,
        "entries": [
            {"change_type": "confirmed", "previous_finding": {"report_id": PROSPECT_REPORT_ID, "finding_id": "fn_market_visibility_gap", "statement": "Three relevant competitors appear ahead of the business for the confirmed local queries.", "fingerprint": "sha256:" + "a" * 64}, "current_finding_ids": ["fn_market_visibility_gap"], "evidence_ids": ["ev_gsc_low_ctr"], "reason": "Search Console impressions confirm the market opportunity behind the public SERP observation."},
            {"change_type": "reprioritized", "previous_finding": {"report_id": PROSPECT_REPORT_ID, "finding_id": "fn_service_page_gap", "statement": "The site lacks a dedicated page for the primary emergency plumbing opportunity.", "fingerprint": "sha256:" + "b" * 64}, "current_finding_ids": ["fn_service_page_gap"], "evidence_ids": ["ev_ga4_low_engagement"], "reason": "GA4 engagement evidence increases the blocking power of the missing dedicated page."},
            {"change_type": "refined", "previous_finding": {"report_id": PROSPECT_REPORT_ID, "finding_id": "fn_public_gbp_service_gap", "statement": "The public GBP service set does not explicitly describe the primary emergency plumbing service.", "fingerprint": "sha256:" + "c" * 64}, "current_finding_ids": ["fn_public_gbp_service_gap"], "evidence_ids": ["ev_gbp_website_clicks"], "reason": "Managed GBP Performance adds a measurable baseline to the previously public-only finding."},
        ],
    }
    report["limitations"] = [
        {"limitation_id": "lim_gsc_privacy", "category": "provider", "severity": "low", "description": "Search Console omits some privacy-filtered queries.", "affected_sections": ["first_party_performance", "findings"]}
    ]
    return report
