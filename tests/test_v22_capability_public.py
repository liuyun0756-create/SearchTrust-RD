"""Actual rule execution on synthetic saved snapshots, not hand-authored reports."""
from copy import deepcopy
from datetime import timedelta
from uuid import UUID

import pytest

from app.jobs_v22.digest import canonical_json_bytes, verified_request_digest
from app.report_v22.capability import AnalysisInputManifest
from app.report_v22.capability_public import RULESET, CapabilityPublicError, build_public_capability_report
from app.report_v22.evidence_models import EvidenceBuildInput
from app.report_v22.findings_models import PublicFindingsInput
from evidence_helpers import NOW, context
from findings_helpers import collection, market, site
from public_gbp_helpers import public_source, sample_input


def inputs(records=None, *, search_gap=False, business_source=None):
    gbp = sample_input()
    serp = market() if search_gap else market([("example.test", 1, "provider_position")])
    competitor = collection(serp, types=("home", "home", "home"))
    sources = [business_source or site(records), serp, competitor, public_source(gbp)]
    for source in sources:
        source.binding.expires_at = source.binding.expires_at or NOW + timedelta(days=1)
        source.binding.health_status = "healthy"
        source.binding.identity_match_status = "matched"
    ctx = context(customer_public_gbp=gbp["reference"])
    request = PublicFindingsInput(evidence_input=EvidenceBuildInput(context=ctx, sources=sources))
    if business_source:
        from test_v22_site_gbp_alignment import identity
        request.business_identity = identity(request.evidence_input)
    stamp = lambda d: d.isoformat().replace("+00:00", "Z")
    manifest = dict(schema_version="v22_analysis_input_v1", id=str(UUID(int=901)), job_id=str(UUID(int=902)),
        user_id=str(UUID(int=903)), case_id=str(ctx.case_id), mode="acquisition", ruleset_version=RULESET,
        generation_input_checksum=verified_request_digest(request.model_dump(mode="json")),
        evaluated_at=stamp(NOW), previous_report=None, implementation=None,
        business=dict(id=str(UUID(int=904)), user_id=str(UUID(int=903)), case_id=str(ctx.case_id), revision=1,
            site_url=str(ctx.site_url), primary_service=ctx.primary_service, target_market=ctx.target_market.display_name,
            target_pages=[], competitor_sites=[str(c.website_url) for c in ctx.competitors], priority=None,
            confirmation_source="seo_confirmation", confirmed_at=stamp(NOW-timedelta(days=1))),
        snapshots=[dict(snapshot_id=str(s.binding.snapshot_id), case_id=str(ctx.case_id),
            source_kind="public_gbp" if s.kind == "public_gbp" else s.binding.source_type,
            schema_version=s.binding.schema_version, payload_checksum=s.binding.payload_checksum,
            fetched_at=stamp(s.binding.fetched_at), expires_at=stamp(s.binding.expires_at), health="healthy",
            identity_match="matched", binding_id=None, scope=f"Saved {s.kind} sample for Austin") for s in sources])
    manifest["input_checksum"] = verified_request_digest(manifest)
    return AnalysisInputManifest.model_validate_json(canonical_json_bytes(manifest)), request


@pytest.mark.parametrize("count,records,gap", [
    (0, [{"title": "Plumbing"}], False),
    (1, [{"status_code": 503}], False),
    (2, [{"status_code": 503}, {"title": "Shared"}, {"title": "Shared"}], False),
    (3, [{"status_code": 503}, {"title": "Shared"}, {"title": "Shared"}], True),
])
def test_actual_rules_deliver_zero_to_three_actions_without_filler(count, records, gap):
    manifest, request = inputs(records, search_gap=gap)
    before = request.model_dump(mode="json")
    report = build_public_capability_report(manifest, request)
    assert len(report.actions) == count
    assert report.action_state == ("actions_supported" if count else "insufficient_basis")
    assert report.settlement_policy == "one_credit_per_completed_analysis"
    assert request.model_dump(mode="json") == before
    assert report == build_public_capability_report(manifest, request)
    assert report.evidence and report.findings and not report.changes
    assert all(a.materials and a.target for a in report.actions)
    assert all(m.readiness != "ready_to_use" for a in report.actions for m in a.materials)


def test_customer_specific_values_change_the_observation_and_material_not_just_the_cover():
    m1, r1 = inputs([{"status_code": 503}])
    m2, r2 = inputs([{"status_code": 404}])
    a, b = build_public_capability_report(m1, r1), build_public_capability_report(m2, r2)
    assert "503" in a.summary and "404" in b.summary
    assert a.summary != b.summary
    assert "503" in a.actions[0].materials[0].body
    assert "404" in b.actions[0].materials[0].body
    assert any(e.value == 503 for e in a.evidence)
    assert all(e.snapshot_id in {s.snapshot_id for s in m1.snapshots} for e in a.evidence)
    assert all(e.field_path.startswith("/payload/") for e in a.evidence)


def test_title_material_quotes_actual_titles_but_does_not_invent_publishable_business_copy():
    manifest, request = inputs([{"title": "Emergency Care"}, {"title": "Emergency Care"}])
    report = build_public_capability_report(manifest, request)
    material = next(m for a in report.actions for m in a.materials if m.kind == "title_brief")
    assert "emergency care" in material.body
    assert material.readiness == "requires_confirmation" and material.confirmation_items
    assert "24/7" not in material.body and "guaranteed" not in material.body


@pytest.mark.parametrize("mutation", ["snapshot", "checksum", "payload", "business", "case", "ruleset", "expiry", "missing_source"])
def test_frozen_basis_cannot_be_swapped_or_silently_regenerated(mutation):
    manifest, request = inputs([{"status_code": 503}])
    # Mutating parsed instances must not bypass validation.
    if mutation == "snapshot": request.evidence_input.sources[0].binding.snapshot_id = UUID(int=999)
    if mutation == "checksum": request.evidence_input.sources[0].binding.payload_checksum = "sha256:"+"0"*64
    if mutation == "payload": request.evidence_input.sources[0].payload.pages[0].status_code = 404
    if mutation == "business": request.evidence_input.context.primary_service = "Another service"
    if mutation == "case": request.evidence_input.context.case_id = UUID(int=999)
    if mutation == "ruleset": manifest.ruleset_version = "unapproved-rules"
    if mutation == "expiry": request.evidence_input.sources[0].binding.expires_at += timedelta(hours=1)
    if mutation == "missing_source": request.evidence_input.sources.pop()
    with pytest.raises(CapabilityPublicError, match="^CAPABILITY_PUBLIC_INPUT_OR_RESULT_INVALID$"):
        build_public_capability_report(manifest, request)


def test_report_limits_fail_closed_without_partial_or_empty_success():
    manifest, request = inputs([{"status_code": 503} for _ in range(51)])
    with pytest.raises(CapabilityPublicError): build_public_capability_report(manifest, request)


def test_source_reordering_cannot_reinterpret_frozen_request():
    manifest, request = inputs([{"status_code": 503}, {"title": "Shared"}, {"title": "Shared"}])
    first = build_public_capability_report(manifest, request)
    shuffled = deepcopy(request); shuffled.evidence_input.sources.reverse()
    # The manifest freezes exact request bytes, including source order. A caller
    # cannot reinterpret a frozen request even if the rules might be equivalent.
    with pytest.raises(CapabilityPublicError): build_public_capability_report(manifest, shuffled)
    assert build_public_capability_report(manifest, request) == first


def test_profile_material_uses_the_actual_mismatched_phone_without_choosing_an_authoritative_value():
    from site_business_helpers import business_html, source
    manifest, request = inputs(business_source=source(business_html(telephone="+1 512 555 9999")))
    report = build_public_capability_report(manifest, request)
    phone = next(f for f in report.findings if f.rule_id.endswith("phone_alignment"))
    values = {e.value for e in report.evidence if e.id in phone.evidence_ids}
    assert "+1 512 555 9999" in values and "+1 512 555 0100" in values
    material = next(m for a in report.actions for m in a.materials if m.kind == "profile_correction")
    assert material.readiness == "requires_confirmation"
    assert "authoritative" in material.body and "call-tracking" in material.body
    assert not any(m.readiness == "ready_to_use" for a in report.actions for m in a.materials)
