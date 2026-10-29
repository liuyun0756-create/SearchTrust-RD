"""Conditional page-title review over complete, frozen synthetic observations."""
from copy import deepcopy
from datetime import datetime

import pytest

from app.collectors.site_inventory_models import DeepPageSnapshot, SelectedPageRecord
from app.jobs_v22.digest import canonical_json_bytes, request_digest, verified_request_digest
from app.report_v22.capability import AnalysisInputManifest
from app.report_v22.capability_public import build_public_capability_report
from app.report_v22.page_title_review import RULE
from findings_helpers import seal, site
from test_v22_capability_public import inputs
from test_v22_site_gbp_alignment import identity


def title_inputs(*, pages=None, deep=True, target_pages=None, ruleset="v22_capability_public_v5"):
    records = pages or [dict(url="https://example.test/plumbing-repair",
        final_url="https://example.test/plumbing-repair", canonical_url="https://example.test/plumbing-repair",
        title="We tackle your plumbing problems | Fixture Plumbing LLC",
        h1="Plumbing repairs and upgrades")]
    source = site(records)
    if deep:
        for page in source.payload.pages:
            source.payload.selected_pages.append(SelectedPageRecord(url=page.url, page_type=page.page_type,
                crawl_depth=page.crawl_depth, selection_score=100, selection_reasons=["service_review"],
                deep_analyzed=True, deep_snapshot=DeepPageSnapshot(url=page.url, final_url=page.final_url,
                    page_type=page.page_type, crawl_depth=page.crawl_depth, collected_at=source.payload.completed_at,
                    status_code=page.status_code, content_type=page.content_type, response_bytes=100,
                    content_checksum=request_digest("synthetic plumbing repairs"), html="<p>Plumbing repairs</p>",
                    text="Plumbing repairs and upgrades. Contact Fixture Plumbing.", title=page.title, h1=page.h1)))
        source.payload.deep_analyzed_count = len(records)
    seal(source)
    manifest, request = inputs()
    request.evidence_input.sources[0] = source
    request.evidence_input.context.primary_service = "Plumbing repair"
    request.business_identity = identity(request.evidence_input)
    raw = manifest.model_dump(mode="json")
    raw["ruleset_version"] = ruleset
    raw["business"].update(primary_service="Plumbing repair", target_pages=target_pages or [])
    return reseal(raw, request)


def reseal(manifest, request):
    raw = deepcopy(manifest.model_dump(mode="json") if isinstance(manifest, AnalysisInputManifest) else manifest)
    source = request.evidence_input.sources[0]
    seal(source)
    # These fixture bindings retain the original expiry/eligibility contract.
    original = raw["snapshots"][0]
    source.binding.expires_at = datetime.fromisoformat(original["expires_at"].replace("Z", "+00:00"))
    source.binding.health_status = "healthy"
    source.binding.identity_match_status = "matched"
    original["payload_checksum"] = source.binding.payload_checksum
    raw["generation_input_checksum"] = verified_request_digest(request.model_dump(mode="json"))
    raw["input_checksum"] = verified_request_digest({k: v for k, v in raw.items() if k != "input_checksum"})
    return AnalysisInputManifest.model_validate_json(canonical_json_bytes(raw)), request


def title_actions(report):
    ids = {f.id for f in report.findings if f.rule_id == RULE}
    return [a for a in report.actions if set(a.finding_ids) & ids]


def test_frozen_title_and_h1_produce_conditional_review_not_publishable_copy():
    manifest, request = title_inputs()
    before = deepcopy(request.model_dump(mode="json"))
    report = build_public_capability_report(manifest, request)
    action, = title_actions(report)
    material, = action.materials
    assert action.target == "https://example.test/plumbing-repair"
    assert material.kind == "title_brief" and material.readiness == "requires_confirmation"
    assert "Current title: We tackle your plumbing problems" in material.body
    assert "Current H1: Plumbing repairs and upgrades" in material.body
    assert "Draft for SEO approval: Plumbing repair | Fixture Plumbing LLC" in material.body
    assert "Keep the current title" in material.body
    assert "appearance/title-link" in material.body
    evidence = {e.id: e for e in report.evidence}
    assert {evidence[key].value for key in material.evidence_ids} == {
        "We tackle your plumbing problems | Fixture Plumbing LLC", "Plumbing repairs and upgrades"}
    assert all(evidence[key].field_path.startswith("/payload/pages/0/") for key in material.evidence_ids)
    finding, = [f for f in report.findings if f.rule_id == RULE]
    assert finding.interpretation == "needs_verification"
    assert "not proof" in finding.limitations[0]
    assert "No action is proposed" not in report.summary
    assert report == build_public_capability_report(manifest, request)
    assert request.model_dump(mode="json") == before


@pytest.mark.parametrize("mutation", ["descriptive", "noindex", "canonical", "no_deep", "deep_title", "deep_h1",
    "deep_non_html", "deep_body", "off_site", "no_identity", "unrelated_h1", "unrelated_path", "http_error"])
def test_ineligible_or_already_descriptive_pages_do_not_receive_the_review(mutation):
    manifest, request = title_inputs(deep=mutation != "no_deep")
    source = request.evidence_input.sources[0]
    page = source.payload.pages[0]
    deep = source.payload.selected_pages[0].deep_snapshot if source.payload.selected_pages else None
    if mutation == "descriptive": page.title = deep.title = "Plumbing repairs | Fixture Plumbing"
    if mutation == "noindex": page.meta_robots = ["NOINDEX, follow"]
    if mutation == "canonical": page.canonical_url = "https://example.test/another"
    if mutation == "deep_title": deep.title = "Different capture"
    if mutation == "deep_h1": deep.h1 = "Different capture"
    if mutation == "deep_non_html": deep.content_type = "application/pdf"
    if mutation == "deep_body": deep.text = "Unrelated service"
    if mutation == "off_site": page.final_url = deep.final_url = "https://another.test/plumbing-repair"
    if mutation == "no_identity": request.business_identity = None
    if mutation == "unrelated_h1": page.h1 = deep.h1 = "Heating installation"
    if mutation == "unrelated_path": page.final_url = deep.final_url = "https://example.test/blog"
    if mutation == "http_error": page.status_code = deep.status_code = 503
    manifest, request = reseal(manifest, request)
    assert not title_actions(build_public_capability_report(manifest, request))


def test_only_confirmed_target_is_reviewed_when_scope_names_a_different_page():
    manifest, request = title_inputs(target_pages=["https://example.test/other"])
    assert not title_actions(build_public_capability_report(manifest, request))


def test_existing_duplicate_title_action_is_not_duplicated_for_same_target():
    pages = [dict(url=f"https://example.test/{path}", final_url=f"https://example.test/{path}",
        canonical_url=f"https://example.test/{path}", title="Generic service", h1="Plumbing repairs")
        for path in ["plumbing-repair", "other"]]
    manifest, request = title_inputs(pages=pages)
    report = build_public_capability_report(manifest, request)
    assert any(m.kind == "title_brief" for a in report.actions for m in a.materials)
    assert not title_actions(report)


def test_v4_replay_keeps_same_title_observations_without_new_review():
    manifest, request = title_inputs(ruleset="v22_capability_public_v4")
    report = build_public_capability_report(manifest, request)
    assert not title_actions(report)
    assert report.action_state == "insufficient_basis"
    assert report.prioritization.rules_version == "v22_business_priority_v2"


def test_confirmed_target_can_review_a_service_page_with_a_nonmatching_slug():
    manifest, request = title_inputs(target_pages=["https://example.test/repairs"])
    source = request.evidence_input.sources[0]
    source.payload.pages[0].final_url = source.payload.pages[0].canonical_url = "https://example.test/repairs"
    source.payload.selected_pages[0].deep_snapshot.final_url = "https://example.test/repairs"
    manifest, request = reseal(manifest, request)
    action, = title_actions(build_public_capability_report(manifest, request))
    assert action.target == "https://example.test/repairs"


def test_ambiguous_service_targets_need_confirmation_instead_of_multiple_guesses():
    pages = [dict(url=f"https://example.test/{prefix}/plumbing-repair",
        final_url=f"https://example.test/{prefix}/plumbing-repair",
        canonical_url=f"https://example.test/{prefix}/plumbing-repair",
        title=f"Help with pipes for {prefix}", h1="Plumbing repairs") for prefix in ["homes", "shops"]]
    manifest, request = title_inputs(pages=pages)
    report = build_public_capability_report(manifest, request)
    assert not title_actions(report)
    assert any("Several saved pages" in line for line in report.limitations)
    manifest.business.target_pages = ["https://example.test/homes/plumbing-repair"]
    manifest, request = reseal(manifest, request)
    action, = title_actions(build_public_capability_report(manifest, request))
    assert action.target == "https://example.test/homes/plumbing-repair"


def test_review_targets_the_captured_final_url_after_a_same_page_redirect():
    manifest, request = title_inputs()
    source = request.evidence_input.sources[0]
    source.payload.pages[0].final_url = "https://example.test/plumbing-repair/"
    source.payload.selected_pages[0].deep_snapshot.final_url = "https://example.test/plumbing-repair/"
    manifest, request = reseal(manifest, request)
    action, = title_actions(build_public_capability_report(manifest, request))
    assert action.target == "https://example.test/plumbing-repair/"
    assert "Page: https://example.test/plumbing-repair/\n" in action.materials[0].body
