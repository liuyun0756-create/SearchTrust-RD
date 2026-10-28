"""Actual public, first-party and cross-source rules on fixed saved snapshots."""
from copy import deepcopy
from datetime import date, timedelta
import json
import re
from uuid import UUID

import pytest

from app.jobs_v22.digest import canonical_json_bytes, verified_request_digest
from app.report_v22.capability import AnalysisInputManifest
from app.report_v22.capability_public import build_public_capability_report
from app.report_v22.capability_service import RULESET, CapabilityServiceError, build_service_capability_report
from app.report_v22.capability_service_inputs import CapabilityServiceInput
from app.report_v22.first_party_findings import build_first_party_findings
from app.report_v22.first_party_findings_errors import FirstPartyFindingsError
from test_v22_capability_public import inputs as public_inputs
from test_v22_cross_source_findings import aligned_values
from test_v22_first_party_findings import trusted


def _shift_dates(value):
    if isinstance(value, dict): return {key: _shift_dates(child) for key,child in value.items()}
    if isinstance(value, list): return [_shift_dates(child) for child in value]
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}",value):
        return (date.fromisoformat(value)-timedelta(days=7)).isoformat()
    return value


def inputs(*, mode="first_service", conflict=False, not_set=False, records=None, clean=False):
    public_manifest, public = public_inputs(records)
    gsc, ga4 = aligned_values(conflict=conflict)
    if clean:
        for row in [*gsc.current.queries.rows, *gsc.current.pages.rows]:
            row.clicks=50; row.impressions=100; row.ctr=.5; row.position=1
        for view in ("totals","queries","pages"):
            getattr(gsc.previous,view).rows=deepcopy(getattr(gsc.current,view).rows)
        for row in ga4.current.landing_pages.rows:
            row.engagement_rate=.6; row.engaged_sessions=int(row.sessions*.6)
            row.key_events=int(row.sessions*.1); row.session_key_event_rate=.1
        ga4.previous.landing_pages.rows=deepcopy(ga4.current.landing_pages.rows)
        ga4.previous.totals.rows=deepcopy(ga4.current.totals.rows)
    if not_set:
        from test_v22_first_party_findings import landing
        ga4.current.landing_pages.rows.append(landing("(not set)",30,.4,0,0))
    snapshots = []
    for kind, value, number in (("gsc", gsc, 901), ("ga4",ga4,902)):
        shifted = type(value).model_validate_json(json.dumps(_shift_dates(value.model_dump(mode="json"))))
        snapshot = trusted(kind, shifted, number)
        snapshot.case_id = public_manifest.case_id
        snapshot.fetched_at = public.evidence_input.context.evaluated_at-timedelta(hours=1)
        snapshot.expires_at = public.evidence_input.context.evaluated_at+timedelta(days=1)
        snapshots.append(snapshot)
    parent = build_public_capability_report(public_manifest, public).model_dump(mode="json") if mode == "after_acquisition" else None
    request = CapabilityServiceInput.model_validate_json(canonical_json_bytes(dict(public_input=public.model_dump(mode="json"),
        first_party_input=dict(case_id=str(public_manifest.case_id), parent_report_id=parent["id"] if parent else None,
            evaluated_at=public_manifest.evaluated_at, snapshots=[s.model_dump(mode="json") for s in snapshots]))))
    raw = public_manifest.model_dump(mode="json")
    raw.update(id=str(UUID(int=9991)),job_id=str(UUID(int=9992)),mode=mode,ruleset_version=RULESET,
        generation_input_checksum=verified_request_digest(request.model_dump(mode="json")))
    if parent:
        raw["previous_report"] = dict(report_id=parent["id"],case_id=raw["case_id"], report_type="prospect",version=1,payload_checksum=verified_request_digest(parent))
    for snapshot in snapshots:
        raw["snapshots"].append(dict(snapshot_id=str(snapshot.snapshot_id),case_id=raw["case_id"],source_kind=snapshot.source_type,
            schema_version=snapshot.schema_version,payload_checksum=snapshot.payload_checksum,
            fetched_at=snapshot.fetched_at.isoformat().replace("+00:00","Z"),expires_at=snapshot.expires_at.isoformat().replace("+00:00","Z"),
            health="healthy",identity_match="matched",binding_id=str(snapshot.binding_id),scope=f"Confirmed {snapshot.source_type} resource for example.test"))
    raw["input_checksum"] = verified_request_digest({k:v for k,v in raw.items() if k!="input_checksum"})
    return AnalysisInputManifest.model_validate_json(canonical_json_bytes(raw)),request,parent


@pytest.mark.parametrize("mode",["first_service","after_acquisition"])
def test_actual_service_rules_preserve_values_scope_and_real_or_absent_history(mode):
    manifest, request, parent = inputs(mode=mode)
    before = request.model_dump(mode="json")
    report = build_service_capability_report(manifest,request,previous_report=parent)
    assert report.report_type == "verified_execution" and report.version == (2 if parent else 1)
    assert 1 <= len(report.actions) <= 3
    assert {"gsc","ga4","site"} <= {e.source_kind for e in report.evidence}
    assert any(e.value == 750 and e.source_kind == "gsc" for e in report.evidence)
    assert any(e.value == 60 and e.source_kind == "ga4" for e in report.evidence)
    assert any("2026-08-29" in e.comparison_scope for e in report.evidence if e.source_kind in {"gsc","ga4"})
    assert any("CONFIRMED_SEARCH_OPPORTUNITY" in f.rule_id for f in report.findings)
    assert not report.changes
    assert (request.first_party_input.parent_report_id is None) == (mode=="first_service")
    assert report.settlement_policy == "one_credit_per_completed_analysis"
    assert request.model_dump(mode="json") == before
    assert report == build_service_capability_report(manifest,request,previous_report=parent)


def test_healthy_but_unresolved_landing_page_produces_scoped_measurement_material():
    manifest, request, _ = inputs(not_set=True)
    report = build_service_capability_report(manifest,request)
    findings = {f.id:f for f in report.findings}
    action = next(a for a in report.actions if any("LANDING_PAGE_NOT_SET" in findings[key].rule_id for key in a.finding_ids))
    material = action.materials[0]
    assert material.kind == "measurement_check" and material.readiness == "requires_confirmation"
    assert "(not set)" in material.body and "event meaning" in material.body
    assert "qualified leads" in " ".join(report.limitations)


def test_conflicting_sources_stay_conflicting_not_an_automatic_content_fix():
    manifest,request,_ = inputs(conflict=True)
    report = build_service_capability_report(manifest,request)
    conflict = next(f for f in report.findings if f.rule_id.endswith("PAGE_DIRECTION_CONFLICT"))
    assert conflict.interpretation == "conflicting"
    assert {e.source_kind for e in report.evidence if e.id in conflict.evidence_ids} == {"gsc","ga4"}
    action = next(a for a in report.actions if conflict.id in a.finding_ids)
    assert action.materials[0].kind == "measurement_check"
    assert "before changing page content" in conflict.judgment


@pytest.mark.parametrize("resign",[False,True])
@pytest.mark.parametrize("field",["binding_id","snapshot_id","case_id","payload_checksum","health_status","identity_match_status","fetched_at","normalized_payload","source_scope","input_checksum"])
def test_private_sources_and_frozen_analysis_inputs_cannot_be_swapped(field,resign):
    manifest,request,_ = inputs()
    source = request.first_party_input.snapshots[0]
    if field in {"binding_id","snapshot_id","case_id"}: setattr(source,field,UUID(int=123456))
    elif field=="payload_checksum": source.payload_checksum="sha256:"+"0"*64
    elif field=="health_status": source.health_status="unhealthy"
    elif field=="identity_match_status": source.identity_match_status="mismatched"
    elif field=="fetched_at": source.fetched_at-=timedelta(days=1)
    elif field=="normalized_payload": source.normalized_payload["current"]["totals"]["rows"][0]["clicks"]=999
    elif field=="source_scope": request.public_input.evidence_input.context.target_market.display_name="Another city"
    else: manifest.input_checksum="sha256:"+"0"*64
    if resign and field!="input_checksum":
        manifest.generation_input_checksum=verified_request_digest(request.model_dump(mode="json"))
        manifest.input_checksum=verified_request_digest(manifest.model_dump(mode="json",exclude={"input_checksum"}))
    with pytest.raises(CapabilityServiceError,match="^CAPABILITY_SERVICE_INPUT_OR_RESULT_INVALID$"):
        build_service_capability_report(manifest,request)


def test_no_fake_parent_and_after_acquisition_requires_the_exact_previous_report():
    manifest,request,_ = inputs()
    with pytest.raises(FirstPartyFindingsError): build_first_party_findings(request.first_party_input)
    with pytest.raises(CapabilityServiceError): build_service_capability_report(manifest,request,previous_report={})
    manifest,request,parent = inputs(mode="after_acquisition")
    with pytest.raises(CapabilityServiceError): build_service_capability_report(manifest,request)
    changed=deepcopy(parent);changed["summary"]="Changed after freezing"
    with pytest.raises(CapabilityServiceError): build_service_capability_report(manifest,request,previous_report=changed)


def test_public_technical_dependencies_remain_closed_after_merging_service_work():
    manifest,request,_ = inputs(records=[{"status_code":503},{"title":"Shared"},{"title":"Shared"}])
    report = build_service_capability_report(manifest,request)
    assert report.actions[0].materials[0].title == "Response and indexing check"
    prior=set()
    for action in report.actions:
        assert set(action.dependencies) <= prior
        prior.add(action.id)


def test_zero_action_service_diagnosis_retains_actual_private_totals_and_does_not_invent_work():
    manifest,request,_=inputs(clean=True)
    report=build_service_capability_report(manifest,request)
    assert report.actions==[] and report.action_state=="insufficient_basis"
    assert report.settlement_policy=="one_credit_per_completed_analysis"
    assert any(e.value==1000 and e.source_kind=="ga4" for e in report.evidence)
    assert {"gsc","ga4","site"} <= {e.source_kind for e in report.evidence}
    assert "No action is proposed" in report.summary


@pytest.mark.parametrize("ruleset", ["v22_capability_service_v1", RULESET])
def test_same_page_measurement_work_is_grouped_and_not_set_is_not_a_live_target(ruleset):
    manifest,request,_=inputs(not_set=True)
    raw = manifest.model_dump(mode="json")
    raw["ruleset_version"] = ruleset
    raw["input_checksum"] = verified_request_digest({k:v for k,v in raw.items() if k != "input_checksum"})
    manifest = AnalysisInputManifest.model_validate_json(canonical_json_bytes(raw))
    report=build_service_capability_report(manifest,request)
    keys=[(a.title,a.target) for a in report.actions]
    assert len(keys)==len(set(keys))
    assert not any(a.target=="(not set)" for a in report.actions)
    if report.prioritization:
        # P4 may retain this candidate outside the three-action budget. Grouping
        # still preserves both observations instead of making duplicate work.
        contact=next(e for e in report.prioritization.candidates if e.input.target=="https://example.test/service"
            and "measurement_check" in e.input.material_kinds and e.prerequisite=="none")
        assert len(contact.input.finding_ids)==2
        facts=" ".join(f.observed_fact for f in report.findings if f.id in contact.input.finding_ids)
        assert "engagement rate" in facts and "key-event" in facts
    else:
        contact=next(a for a in report.actions if a.title=="Check the landing-page contact journey")
        assert len(contact.finding_ids)==2
        assert "engagement rate" in contact.materials[0].body and "key-event" in contact.materials[0].body


def test_tracking_url_aliases_do_not_produce_duplicate_content_work():
    manifest,request,_=inputs()
    report=build_service_capability_report(manifest,request)
    briefs=[a for a in report.actions if a.materials[0].kind=="service_brief"]
    assert len(briefs)==1 and briefs[0].target=="https://example.test/service"
    assert len(briefs[0].finding_ids)==2
    assert any("utm_source=x" in e.object for e in report.evidence)
    assert any("gclid=x" in e.object for e in report.evidence)
