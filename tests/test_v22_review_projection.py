from copy import deepcopy
from uuid import UUID
import json

import pytest

from app.jobs_v22.digest import verified_request_digest
from app.jobs_v22.errors import DeterministicJobError
from app.report_v22.review_evidence_models import ReviewEvidenceBundle
from app.report_v22.review_projection import build_review_projection, parse_review_bundle
from app.report_v22.review_projection_diff import compare_review_projections
from tests.support.review_projection_fixtures import bundle_raw, add_decision, seal


def resign(value):
    value["checksum"] = verified_request_digest({k: v for k, v in value.items() if k != "checksum"})
    return value


def test_four_sections_are_deterministic_and_do_not_promote_effect():
    raw = bundle_raw()
    first = build_review_projection(raw)
    assert first == build_review_projection(json.dumps(raw))
    assert first == build_review_projection(parse_review_bundle(raw))
    a = first.seo_actions[0]
    assert a.facts.recorded_work.state == "seo_recorded"
    assert a.facts.page.comparison == "first_observation"
    assert a.facts.gsc.state == "comparable"
    assert a.facts.ga4.difference == 25
    assert a.next_decision.decision_state == "needs_review"
    assert a.decision_record is None and first.effect is None and not first.causal_attribution
    assert a.facts.ga4.measurement_history == "unverified"
    raw["implementation"]["entries"][0]["note"] = "later mutation"
    assert first == build_review_projection(bundle_raw())
    a.facts.checks.clear()
    assert first.client_actions[0].facts.checks  # no cross-layer aliases


@pytest.mark.parametrize("before,after,delta,percent", [(100,125,25,"25.00"),(100,75,-25,"-25.00"),
    (100,100,0,"0.00"),(0,0,0,None),(0,10,10,None),(3,4,1,"33.33")])
def test_session_counts_keep_existing_arithmetic(before,after,delta,percent):
    a = build_review_projection(bundle_raw(before=before,after=after)).client_actions[0]
    assert (a.facts.ga4.difference, a.facts.ga4.relative_change_percent) == (delta,percent)
    assert a.next_decision.disposition is None


@pytest.mark.parametrize("disposition", ["continue","adjust","observe","defer","new"])
def test_explicit_seo_decisions_remain_statements(disposition):
    raw = add_decision(bundle_raw(),disposition,"new-work" if disposition=="new" else "ac-1")
    p = build_review_projection(raw)
    decision = p.client_new_decisions[0] if disposition=="new" else p.client_actions[0].next_decision
    assert decision.disposition == disposition and decision.decision_state == "seo_recorded"
    assert not decision.system_completion_verified
    client = json.dumps([a.model_dump(mode="json") for a in p.client_actions]+[d.model_dump(mode="json") for d in p.client_new_decisions])
    assert raw["decisions"][0]["reason"] not in client
    for field in ("user_id","checksum","decision_record","implementation_entry","source_references"):
        assert f'"{field}"' not in client


def test_approved_edits_not_generated_text_and_private_notes_are_not_published():
    raw = bundle_raw(sources=False)
    raw["delivery"]["actions"][0]["title"] = "Approved contact clarification"
    raw["delivery_checksum"] = verified_request_digest(raw["delivery"])
    raw["implementation"]["entries"][0]["note"] = "PRIVATE: negotiate contract"
    resign(raw["implementation"])
    p = build_review_projection(seal(raw))
    assert p.client_actions[0].facts.title == "Approved contact clarification"
    assert "PRIVATE:" not in json.dumps([a.model_dump(mode="json") for a in p.client_actions])
    assert "PRIVATE:" in p.seo_actions[0].implementation_entry.note


def test_empty_record_and_absent_sources_do_not_manufacture_values():
    raw = bundle_raw(sources=False)
    raw["implementation"]["entries"] = []
    resign(raw["implementation"])
    p = build_review_projection(seal(raw))
    f = p.client_actions[0].facts
    assert f.recorded_work.state == "not_recorded" and f.page is f.gsc is f.ga4 is None
    assert {c.code for c in f.checks} == {"CONFIRM_RECORDED_WORK","ESTABLISH_PAGE_CHECK","NO_SELECTED_METRICS"}
    assert p.client_actions[0].next_decision.disposition is None


@pytest.mark.parametrize("field", ["user_id","case_id","delivery_id","report_checksum","delivery_checksum"])
def test_resigned_identity_mismatch_rejected(field):
    raw = bundle_raw()
    raw[field] = "sha256:"+"f"*64 if field.endswith("checksum") else str(UUID(int=999))
    with pytest.raises(DeterministicJobError): build_review_projection(seal(raw))


@pytest.mark.parametrize("edit", ["hash","future","order","draft","unknown","selection_action","record_revision","ga4_action","gsc_action","page_action"])
def test_malformed_binding_cannot_be_hidden_as_missing(edit):
    raw = bundle_raw()
    if edit=="hash": raw["checksum"]="sha256:"+"f"*64
    elif edit=="future": raw["evaluated_at"]="2026-10-22T08:00:00Z"
    elif edit=="order": raw["selections"]*=2
    elif edit=="draft": raw["delivery"]["status"]="draft"; raw["delivery_checksum"]=verified_request_digest(raw["delivery"])
    elif edit=="unknown": raw["arbitrary"]="private"
    elif edit=="selection_action": raw["selections"][0]["action_id"]="other"
    elif edit=="record_revision": raw["implementation"]["revision"]+=1; resign(raw["implementation"])
    elif edit=="ga4_action": raw["selections"][0]["ga4"]["selection"]["action_id"]="other"
    elif edit=="gsc_action": raw["selections"][0]["gsc"]["request"]["action_id"]="other"
    else:
        p=raw["selections"][0]["page"]["current"]
        p["plan"]["binding"]["action_id"]="other"; resign(p["plan"]); resign(p)
    if edit!="hash": seal(raw)
    with pytest.raises(DeterministicJobError) as e: build_review_projection(raw)
    assert e.value.error_code=="V22_REVIEW_INVALID"


@pytest.mark.parametrize("edit", ["blank","too_long","control","references","duplicate","future","before_evidence","completion","unknown_action","too_many","wrong_hash"])
def test_decision_guards(edit):
    raw=add_decision(bundle_raw())
    d=raw["decisions"][0]
    if edit=="blank": d["reason"]=" "
    elif edit=="too_long": d["reason"]="x"*1201
    elif edit=="control": d["reason"]="x\x00y"
    elif edit=="references": d["references"]=["page:foreign"]
    elif edit=="duplicate": d["references"]*=2
    elif edit=="future": d["recorded_at"]="2030-01-01T00:00:00Z"
    elif edit=="before_evidence": d["recorded_at"]="2026-10-24T00:00:00Z"
    elif edit=="completion": d["completion_statement"]="unexpected"
    elif edit=="unknown_action": d["action_id"]="other"
    elif edit=="too_many": raw["decisions"]*=4
    else: d["evidence_checksum"]="sha256:"+"f"*64
    resign(raw)
    with pytest.raises(DeterministicJobError): build_review_projection(raw)


def test_observe_requires_implementation_and_new_requires_new_identity():
    raw=add_decision(bundle_raw(sources=False),"observe")
    raw["implementation"]["entries"]=[]; resign(raw["implementation"])
    with pytest.raises(DeterministicJobError): build_review_projection(seal(raw))
    with pytest.raises(DeterministicJobError): build_review_projection(add_decision(bundle_raw(),"new"))


def test_input_size_duplicate_keys_and_mutated_instances_are_checked():
    with pytest.raises(DeterministicJobError) as e: parse_review_bundle(b" "*2_000_001)
    assert e.value.error_code=="V22_REVIEW_TOO_LARGE"
    with pytest.raises(DeterministicJobError): parse_review_bundle('{"x":1,"x":2}')
    b=parse_review_bundle(bundle_raw()); b.implementation.entries.clear()
    with pytest.raises(DeterministicJobError): build_review_projection(b)


def test_diff_first_baseline_and_same_evidence_have_no_fake_changes():
    raw=bundle_raw()
    assert compare_review_projections(raw).state=="first_baseline"
    diff=compare_review_projections(raw,raw)
    assert diff.state=="compared" and all(not a.changed_sections for a in diff.actions)
    old=deepcopy(raw)
    raw["evaluated_at"]="2026-12-17T08:00:00Z"
    p=raw["selections"][0]["page"]["current"]
    p.update(id=str(UUID(int=31100)),observed_at="2026-10-24T08:00:00Z"); resign(p)
    diff=compare_review_projections(seal(raw),old)
    assert all(not a.changed_sections for a in diff.actions)


def test_diff_numbers_are_not_changed_work_decisions():
    diff=compare_review_projections(bundle_raw(after=80),bundle_raw())
    a=diff.actions[0]
    assert a.evidence_changed and not a.decision_changed and a.changed_sections==["ga4"]
    diff=compare_review_projections(add_decision(bundle_raw(),"defer"),bundle_raw())
    assert diff.actions[0].changed_sections==["decision"]


def test_other_approved_basis_is_not_comparable():
    old=bundle_raw(sources=False); raw=deepcopy(old)
    raw["delivery"]["actions"][0]["title"]="Different approved work"
    raw["delivery_checksum"]=verified_request_digest(raw["delivery"])
    assert compare_review_projections(seal(raw),old).state=="not_comparable"


def test_withheld_ga4_preserves_limits_and_does_not_leak_counts():
    raw=bundle_raw(mutate_ga4=lambda s:s["current"]["totals"]["metadata"].update(subject_to_thresholding=True))
    p=build_review_projection(raw)
    assert p.client_actions[0].facts.ga4.state=="withheld"
    assert p.client_actions[0].facts.ga4.before is None
    assert "REVIEW_GA4_LIMITS" in [c.code for c in p.client_actions[0].facts.checks]
