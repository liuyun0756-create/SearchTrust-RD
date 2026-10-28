"""P4 ordinal rules and real frozen-report compatibility. No provider calls."""
from copy import deepcopy
from itertools import permutations

import pytest
from pydantic import ValidationError

from app.jobs_v22.digest import canonical_json_bytes, verified_request_digest
from app.report_v22.capability import AnalysisInputManifest, CapabilityReport, DeliveryRevision
from app.report_v22.capability_priority import candidate_input, prioritize
from app.report_v22.capability_priority import valid_delivery_order
from app.report_v22.capability_public import build_public_capability_report
from app.report_v22.capability_service import build_service_capability_report
from test_v22_capability_public import inputs as public_inputs
from test_v22_capability_service import inputs as service_inputs


def basis():
    return dict(site_url="https://example.test/", target_pages=[], priority=None)


def finding(identifier, **extra):
    return dict(id=identifier, rule_id="test.checked_page", severity="medium", confidence="medium",
        interpretation="supported", classification="fact", impact_path="A clearer contact path can help a visitor reach the business; return is unknown.",
        comparison_scope="Checked page only", **extra)


def candidate(identifier, *, kind="service_brief", target=None, dependencies=(), readiness="requires_confirmation"):
    return dict(action_id=identifier, target=target or f"https://example.test/{identifier}", finding_ids=[identifier],
        dependencies=list(dependencies), material_kinds=[kind], readiness=[readiness])


def changed_manifest(manifest, **changes):
    raw = manifest.model_dump(mode="json")
    for key, value in changes.items():
        if key in {"priority", "target_pages"}: raw["business"][key] = value
        else: raw[key] = value
    raw["input_checksum"] = verified_request_digest({k:v for k,v in raw.items() if k != "input_checksum"})
    return AnalysisInputManifest.model_validate_json(canonical_json_bytes(raw))


def test_same_problems_different_confirmed_focus_change_order_without_revenue_score():
    candidates = [candidate("a", kind="title_brief"), candidate("b", kind="profile_correction")]
    findings = [finding("a"), finding("b")]
    visibility = prioritize(candidates, findings, dict(basis(), priority="service_visibility"), "checksum")
    identity = prioritize(candidates, findings, dict(basis(), priority="business_identity"), "checksum")
    assert visibility["action_order"] == ["a", "b"]
    assert identity["action_order"] == ["b", "a"]
    assert all("return_unknown" in e["reason_codes"] for e in identity["candidates"])
    assert not any("score" in key for e in identity["candidates"] for key in e)


@pytest.mark.parametrize("priority", [None, "Revenue growth of 100%", "business_identity guessed from prose"])
def test_unknown_or_unrecognized_focus_is_not_treated_as_low_value(priority):
    result = prioritize([candidate("a")], [finding("a")], dict(basis(), priority=priority), "checksum")
    e = result["candidates"][0]
    assert e["business_match"] == "unknown" and "not assumed business value" in e["explanation"]
    assert result["business_priority"] == priority


@pytest.mark.parametrize("target,match", [
    ("https://example.test/b", True), ("https://www.example.test:443/b#details", True),
    ("https://example.test/b-extra", False), ("https://other.test/b", False),
    ("https://example.test.evil.test/b", False), ("https://example.test/b?different=1", False),
])
def test_target_matching_is_explicit_same_business_page_not_substring(target, match):
    result = prioritize([candidate("a"), candidate("b", target=target)], [finding("a"), finding("b")],
        dict(basis(), target_pages=["https://example.test/b"]), "checksum")
    assert (result["action_order"][0] == "b") == match


def test_prerequisites_and_dependencies_cannot_be_displaced_by_business_focus():
    candidates = [candidate("a", dependencies=["c"]), candidate("b", kind="measurement_check"), candidate("c", kind="technical_check")]
    findings = [finding("a"), dict(finding("b"), rule_id="V22.FIRST_PARTY.GA4.MEASUREMENT_CONFIGURATION.GA4_NO_CONFIGURED_KEY_EVENTS"),
        dict(finding("c"), rule_id="v22_public.site_http_error")]
    before = deepcopy((candidates, findings))
    expected = prioritize(candidates, findings, dict(basis(), target_pages=["https://example.test/a"]), "checksum")
    assert expected["action_order"] == ["c", "b", "a"]
    assert "dependencies_first" in expected["candidates"][0]["reason_codes"]
    for permuted in permutations(candidates):
        assert prioritize(permuted, list(reversed(findings)), dict(basis(), target_pages=["https://example.test/a"]), "checksum") == expected
    assert (candidates, findings) == before


def test_material_kind_or_display_copy_cannot_manufacture_a_measurement_prerequisite():
    result = prioritize([candidate("a", kind="measurement_check")], [dict(finding("a"), rule_id="V22.FIRST_PARTY.GA4.ENGAGEMENT_GAP")], basis(), "checksum")
    assert result["candidates"][0]["prerequisite"] == "none"


def test_conflicting_evidence_is_retained_and_not_interpreted_as_a_cause():
    result = prioritize([candidate("a"), candidate("b")], [dict(finding("a"), interpretation="conflicting"), finding("b")], basis(), "checksum")
    assert result["action_order"] == ["b", "a"]
    assert result["candidates"][0]["evidence_support"] == "conflicting"
    assert "not a claimed business cause" in result["candidates"][0]["explanation"]


def test_exact_identity_breaks_ties_and_budget_keeps_all_decisions_without_padding():
    candidates = [candidate(k) for k in ("d", "b", "c", "a")]
    result = prioritize(candidates, [finding(k) for k in ("d", "b", "c", "a")], basis(), "checksum")
    assert result["action_order"] == ["a", "b", "c"]
    assert result["candidates"][3]["selected_order"] is None
    assert "outside_delivery_budget" in result["candidates"][3]["reason_codes"]
    assert prioritize([], [], basis(), "checksum")["action_order"] == []


@pytest.mark.parametrize("mutation", ["cycle", "missing", "duplicate"])
def test_invalid_dependency_graph_fails_instead_of_delivering_partial_work(mutation):
    a, b = candidate("a"), candidate("b")
    if mutation == "cycle": a["dependencies"]=["b"]; b["dependencies"]=["a"]
    elif mutation == "missing": a["dependencies"]=["missing"]
    else: b = deepcopy(a)
    with pytest.raises(ValueError): prioritize([a,b], [finding("a"), finding("b")], basis(), "checksum")


def test_actual_service_generation_uses_frozen_focus_and_retains_complete_candidate_audit():
    manifest, request, _ = service_inputs()
    focused = changed_manifest(manifest, target_pages=["https://example.test/service"], priority="service_visibility")
    report = build_service_capability_report(focused, request)
    assert report.actions[0].target == "https://example.test/service"
    assert report.prioritization.input_checksum == focused.input_checksum
    assert report.prioritization.business_priority == "service_visibility"
    assert len(report.prioritization.candidates) > len(report.actions)
    assert report == build_service_capability_report(focused, request)


@pytest.mark.parametrize("mutation", ["missing", "order", "reason", "focus", "selected_input", "excluded_input", "version"])
def test_trace_cannot_contradict_frozen_input_or_delivered_work(mutation):
    manifest, request = public_inputs([{"status_code":503}, {"title":"Shared"}, {"title":"Shared"}], search_gap=True)
    raw = build_public_capability_report(manifest, request).model_dump(mode="json")
    trace = raw["prioritization"]
    if mutation == "missing": raw.pop("prioritization")
    elif mutation == "order": trace["action_order"].reverse()
    elif mutation == "reason": trace["candidates"][0]["explanation"]="Guaranteed revenue improvement."
    elif mutation == "focus": trace["target_pages"]=["https://example.test/invented"]
    elif mutation == "selected_input": raw["actions"][0]["target"]="https://example.test/invented"
    elif mutation == "excluded_input": trace["candidates"][0]["input"]["dependencies"]=["unknown"]
    else: trace["rules_version"]="invented"
    with pytest.raises(ValidationError): CapabilityReport.model_validate_json(canonical_json_bytes(raw))


@pytest.mark.parametrize("kind", ["public", "service"])
def test_frozen_v1_generation_remains_available_and_does_not_add_optional_bytes(kind):
    if kind == "public":
        manifest, request = public_inputs([{"status_code":503}, {"title":"Shared"}, {"title":"Shared"}], search_gap=True)
        report = build_public_capability_report(changed_manifest(manifest, ruleset_version="v22_capability_public_v1"), request)
    else:
        manifest, request, _ = service_inputs()
        report = build_service_capability_report(changed_manifest(manifest, ruleset_version="v22_capability_service_v1"), request)
    raw = report.model_dump(mode="json")
    assert report.prioritization is None and "prioritization" not in raw
    assert canonical_json_bytes(raw) == canonical_json_bytes(CapabilityReport.model_validate_json(canonical_json_bytes(raw)))
    assert verified_request_digest(raw) == {
        "public": "sha256:7235c39edcb1706657764cd59ff6d36cd9575a55cdb98528a39caaf4fca8bb9d",
        "service": "sha256:c8352fd3f685ea9581bce8efc46a582c4fd697448d0d5b6a71fe9ccc8a101b9a",
    }[kind]
    raw["prioritization"] = None
    with pytest.raises(ValidationError): CapabilityReport.model_validate_json(canonical_json_bytes(raw))


def test_seo_reorder_preserves_original_priority_and_does_not_change_approved_report_identity():
    manifest, request, _ = service_inputs()
    report = build_service_capability_report(manifest, request)
    original = report.model_dump(mode="json")
    revision = DeliveryRevision.model_validate(dict(schema_version="v22_delivery_revision_v1", report_id=report.id,
        user_id=manifest.user_id, case_id=manifest.case_id, expected_revision=0, source_checksum=verified_request_digest(original),
        status="approved", title=report.title, summary=report.summary, client_note=None,
        actions=[a.model_dump(exclude={"finding_ids","dependencies"}) for a in reversed(report.actions)]))
    revision.validate_report(report)
    assert report.model_dump(mode="json") == original
    assert revision.actions[0].id != report.prioritization.action_order[0]


@pytest.mark.parametrize("kind", ["public", "service"])
def test_frozen_p4_v2_generation_keeps_the_previous_priority_trace_and_digest(kind):
    if kind == "public":
        m, r = public_inputs([{"status_code":503}, {"title":"Shared"}, {"title":"Shared"}], search_gap=True)
        report=build_public_capability_report(changed_manifest(m,ruleset_version="v22_capability_public_v2"),r)
    else:
        m,r,_=service_inputs()
        report=build_service_capability_report(changed_manifest(m,ruleset_version="v22_capability_service_v2"),r)
    assert report.prioritization.rules_version == "v22_business_priority_v1"
    assert verified_request_digest(report.model_dump(mode="json")) == {
        "public":"sha256:75af3bfd5452b17fcef213ea989562954b8ea2db3289240322e735f18409b6e7",
        "service":"sha256:03db759fe03b47e00fed826c47c472c48ad00859603ec19d2d26d4b1793047ed",
    }[kind]


def test_confirmed_capacity_only_breaks_comparable_ties_and_unknown_is_not_a_blocker():
    candidates=[candidate("a",kind="service_brief",readiness="instructions"),candidate("b",kind="technical_check",readiness="instructions")]
    findings=[finding("a"),finding("b")]
    for capacity,order in [("unknown",["a","b"]),("available",["a","b"]),("limited",["b","a"])]:
        business=dict(basis(),execution_conditions=dict(capacity=capacity,publishing_access="unknown"))
        trace=prioritize(candidates,findings,business,"checksum",rules_version="v22_business_priority_v2")
        assert trace["action_order"]==order
        assert all("publishing_unknown" in e["reason_codes"] for e in trace["candidates"])
    assert prioritize(candidates,[dict(findings[0],severity="critical"),findings[1]],business,"checksum",rules_version="v22_business_priority_v2")["action_order"]==["a","b"]


def test_needs_approval_is_a_condition_not_permission_to_publish():
    trace=prioritize([candidate("a",readiness="ready_to_use")],[finding("a")],
        dict(basis(),execution_conditions=dict(capacity="available",publishing_access="needs_approval")),"checksum",rules_version="v22_business_priority_v2")
    assert trace["candidates"][0]["execution"]=="confirmation_required"
    assert "publishing_needs_approval" in trace["candidates"][0]["reason_codes"]


def test_contact_focus_matches_an_observed_contact_journey_check_not_all_measurement_work():
    inputs=[candidate("a",kind="measurement_check"),candidate("b",kind="technical_check")]
    findings=[dict(finding("a"),rule_id="V22.FIRST_PARTY.GA4.CONVERSION_GAP"),finding("b")]
    trace=prioritize(inputs,findings,dict(basis(),priority="contact_path"),"checksum",rules_version="v22_business_priority_v2")
    assert trace["candidates"][0]["business_match"]=="confirmed_objective"
    assert trace["candidates"][0]["prerequisite"]=="none"
    old=prioritize(inputs,findings,dict(basis(),priority="contact_path"),"checksum")
    assert old["candidates"][0]["business_match"]=="not_matched"


def test_v3_delivery_preserves_layer_and_explicit_dependency_guards():
    m,r=public_inputs([{"status_code":503},{"title":"Shared"},{"title":"Shared"}],search_gap=True)
    report=build_public_capability_report(m,r)
    trace=report.prioritization.model_dump(mode="json")
    order=[a.id for a in report.actions]
    assert valid_delivery_order(order,trace)
    assert not valid_delivery_order(list(reversed(order)),trace)
    revision=dict(schema_version="v22_delivery_revision_v1",report_id=report.id,user_id=m.user_id,case_id=m.case_id,
        expected_revision=0,source_checksum=verified_request_digest(report.model_dump(mode="json")),status="approved",title=report.title,summary=report.summary,client_note=None,
        actions=[a.model_dump(exclude={"finding_ids","dependencies"}) for a in reversed(report.actions)])
    with pytest.raises(ValueError,match="prerequisites"):
        DeliveryRevision.model_validate(revision).validate_report(report)


def test_freezing_execution_conditions_changes_input_identity_not_history():
    m,r,_=service_inputs()
    before=build_service_capability_report(m,r).model_dump(mode="json")
    raw=m.model_dump(mode="json")
    raw["business"]["execution_conditions"]={"capacity":"limited","publishing_access":"needs_approval"}
    raw["input_checksum"]=verified_request_digest({k:v for k,v in raw.items() if k!="input_checksum"})
    updated=AnalysisInputManifest.model_validate_json(canonical_json_bytes(raw))
    report=build_service_capability_report(updated,r)
    assert report.input_manifest.input_checksum != m.input_checksum
    assert "capacity_limited" in report.prioritization.candidates[0].reason_codes
    assert build_service_capability_report(m,r).model_dump(mode="json")==before
