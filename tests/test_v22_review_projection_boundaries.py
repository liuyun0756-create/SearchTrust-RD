"""Adversarial source replay, semantic diff and no-side-effect tests."""
from copy import deepcopy
from uuid import UUID

import httpx
import pytest

from app.jobs_v22.digest import verified_request_digest, canonical_json_bytes
from app.jobs_v22.errors import DeterministicJobError
from app.report_v22.comparison import compare_implementation
from app.report_v22.comparison_models import ComparisonRequest
from app.report_v22.review_projection import build_review_projection
from app.report_v22.review_projection_diff import compare_review_projections
from tests.support.comparison_fixtures import saved_gsc
from tests.support.review_projection_fixtures import bundle_raw, add_decision, seal, align_gsc
from tests.test_v22_review_projection import resign


def replay_gsc(raw):
    g=raw["selections"][0]["gsc"]
    r=ComparisonRequest.model_validate_json(canonical_json_bytes(align_gsc(g["request"])))
    g["selection"].update(days=r.days, scope=r.scope.model_dump(mode="json"),
        before_snapshot_id=str(r.before.snapshot_id) if r.before else None,
        after_snapshot_id=str(r.after.snapshot_id) if r.after else None,
        concurrent_changes=r.concurrent_changes)
    g["result"]=compare_implementation(r).model_dump(mode="json")
    return seal(raw)


@pytest.mark.parametrize("clicks,impressions", [(15,300),(5,100),(10,200),(0,0)])
def test_gsc_direction_does_not_choose_work(clicks,impressions):
    raw=bundle_raw()
    raw["selections"][0]["gsc"]["request"]["after"]=saved_gsc(UUID(raw["case_id"]),after=True,daily_clicks=clicks,daily_impressions=impressions).model_dump(mode="json")
    p=build_review_projection(replay_gsc(raw))
    if impressions:
        assert p.client_actions[0].facts.gsc.metrics[0].after == clicks*28
    else:
        assert p.client_actions[0].facts.gsc.state != "comparable"  # preserve original health eligibility
    assert p.client_actions[0].next_decision.disposition is None
    assert p.client_actions[0].facts.effect is None


@pytest.mark.parametrize("kind",["missing","waiting","scope"])
def test_gsc_ineligible_keeps_original_state(kind):
    raw=bundle_raw(); r=raw["selections"][0]["gsc"]["request"]
    if kind=="missing": r["before"]=None
    elif kind=="waiting": r["days"]=90
    else: r["scope"].update(dimension="page", key="https://example.test/service")
    p=build_review_projection(replay_gsc(raw)); g=p.client_actions[0].facts.gsc
    assert g.state != "comparable" and not g.metrics
    if kind=="waiting":
        assert "WAIT_FOR_WINDOW" in [c.code for c in p.client_actions[0].facts.checks]
    assert p.client_actions[0].facts.ga4.state=="available"  # independent eligibility


@pytest.mark.parametrize("kind",["first_failed","newly_observed","no_longer_observed","same","different_plan","invalid_order"])
def test_page_observations_never_claim_whole_action_complete(kind):
    raw=bundle_raw(); p=raw["selections"][0]["page"]; current=p["current"]
    if kind=="first_failed":
        current.update(status="unable_to_check",failure="page_timeout",failure_stage="page",http_status=None,
            content_checksum=None,media_type=None)
        current["results"][0]["state"]="unable_to_check"
    else:
        p["previous"]=deepcopy(current)
        p["previous"].update(id=str(UUID(int=31200)),observed_at="2026-10-22T12:00:00Z")
        if kind=="newly_observed": p["previous"]["results"][0]["state"]="not_observed"
        elif kind=="no_longer_observed": current["results"][0]["state"]="not_observed"
        elif kind=="different_plan": p["previous"]["plan"]["conditions"][0]["value"]="other"
        elif kind=="invalid_order": p["previous"]["observed_at"]="2026-10-24T12:00:00Z"
        resign(p["previous"]["plan"]); resign(p["previous"])
    resign(current)
    projection=build_review_projection(seal(raw)); f=projection.client_actions[0].facts
    if kind in {"newly_observed","no_longer_observed","same"}:
        assert f.page.changes[0].change == ("unchanged" if kind=="same" else kind)
    elif kind in {"different_plan","invalid_order"}: assert f.page.comparison=="not_comparable"
    else: assert f.page.status=="unable_to_check"
    assert not f.recorded_work.system_completion_verified and f.effect is None


def test_changed_page_and_restricted_metrics_are_real_evidence_diffs():
    old=bundle_raw(); raw=deepcopy(old)
    p=raw["selections"][0]["page"]["current"]; p["results"][0]["state"]="not_observed"; resign(p)
    d=compare_review_projections(seal(raw),old).actions[0]
    assert d.evidence_changed and not d.decision_changed and "page" in d.changed_sections
    limited=bundle_raw(mutate_ga4=lambda s:s["current"]["totals"]["metadata"].update(subject_to_thresholding=True))
    d=compare_review_projections(limited,old).actions[0]
    assert "ga4" in d.changed_sections and "checks" in d.changed_sections and not d.decision_changed


def test_same_source_identity_conflicting_contents_rejected_before_comparison():
    raw=bundle_raw(); p=raw["selections"][0]["page"]
    p["previous"]=deepcopy(p["current"]); p["previous"]["observed_at"]="2026-10-22T12:00:00Z"; resign(p["previous"])
    with pytest.raises(DeterministicJobError): build_review_projection(seal(raw))


def test_resigned_incorrect_comparison_arithmetic_is_replayed():
    raw=bundle_raw(); result=raw["selections"][0]["gsc"]["result"]
    result["metrics"][0]["after"]+=1; resign(result)
    with pytest.raises(DeterministicJobError): build_review_projection(seal(raw))


def test_sources_with_other_implementation_revision_cannot_silently_join():
    raw=bundle_raw(); raw["implementation"]["revision"]+=1; resign(raw["implementation"])
    with pytest.raises(DeterministicJobError): build_review_projection(seal(raw))


def multi_bundle():
    raw=bundle_raw(sources=False)
    for i in (2,3):
        action=deepcopy(raw["report"]["actions"][0]); action["id"]=f"ac-{i}"; raw["report"]["actions"].append(action)
        editable=deepcopy(raw["delivery"]["actions"][0]); editable["id"]=f"ac-{i}"; raw["delivery"]["actions"].append(editable)
        raw["selections"].append(dict(action_id=f"ac-{i}",page=None,gsc=None,ga4=None))
    raw["report_checksum"]=verified_request_digest(raw["report"])
    raw["delivery"]["source_checksum"]=raw["report_checksum"]
    raw["delivery_checksum"]=verified_request_digest(raw["delivery"])
    raw["implementation"]["report_checksum"]=raw["report_checksum"]; resign(raw["implementation"])
    return seal(raw)


def test_three_action_order_decisions_and_no_cross_action_reference():
    raw=multi_bundle()
    p=build_review_projection(raw)
    assert [a.facts.action_id for a in p.seo_actions]==["ac-1","ac-2","ac-3"]
    assert p.client_actions[1].facts.recorded_work.state=="not_recorded"
    wrong=deepcopy(raw); wrong["selections"].reverse()
    with pytest.raises(DeterministicJobError): build_review_projection(seal(wrong))
    wrong=add_decision(deepcopy(raw),"continue","ac-2")  # reference belongs to action 1
    with pytest.raises(DeterministicJobError): build_review_projection(wrong)
    for i in (1,2,3):
        add_decision(raw,"continue",f"ac-{i}")
        raw["decisions"][-1]["references"]=[]
    assert len(build_review_projection(seal(raw)).seo_actions)==3
    add_decision(raw,"new","new-work")
    with pytest.raises(DeterministicJobError): build_review_projection(raw)


def test_no_supported_actions_is_still_valid_diagnostic_input():
    raw=bundle_raw(sources=False)
    raw["report"].update(actions=[],action_state="insufficient_basis")
    raw["report_checksum"]=verified_request_digest(raw["report"])
    raw["delivery"].update(actions=[],source_checksum=raw["report_checksum"])
    raw["delivery_checksum"]=verified_request_digest(raw["delivery"])
    raw["implementation"].update(entries=[],report_checksum=raw["report_checksum"]); resign(raw["implementation"])
    raw["selections"]=[]
    p=build_review_projection(seal(raw))
    assert not p.client_actions and not p.seo_new_decisions
    assert p.prior_diagnosis.findings and p.prior_diagnosis.limitations
    assert p.prior_diagnosis.source=="prior_report_not_rechecked"


def test_pure_build_does_not_call_provider_repositories_queue_or_billing(monkeypatch):
    raw=bundle_raw()
    def forbidden(*args,**kwargs): raise AssertionError("unexpected I/O")
    from app.report_v22.comparison_storage import ComparisonRepository
    from app.report_v22.ga4_collection_storage import Ga4CollectionRepository
    from app.jobs_v22.capability_input_repository import SupabaseCapabilityInputRepository
    monkeypatch.setattr(httpx.Client,"send",forbidden)
    monkeypatch.setattr(httpx.AsyncClient,"send",forbidden)
    monkeypatch.setattr(ComparisonRepository,"_rpc",forbidden)
    monkeypatch.setattr(Ga4CollectionRepository,"read_collection",forbidden)
    monkeypatch.setattr(SupabaseCapabilityInputRepository,"prepare",forbidden)
    assert build_review_projection(raw).schema_version=="v22_review_projection_v1"
    assert compare_review_projections(raw,raw).state=="compared"


def test_old_qualified_snapshot_numbers_survive_transport_without_hash_rewrite():
    raw=bundle_raw(); before=deepcopy(raw)
    build_review_projection(canonical_json_bytes(raw))
    assert raw==before
    snapshot=raw["selections"][0]["gsc"]["request"]["before"]
    assert isinstance(snapshot["normalized_payload"]["current"]["totals"]["rows"][0]["position"],float)


@pytest.mark.parametrize("source", ["gsc", "ga4"])
def test_same_case_does_not_authorize_a_different_website_scope(source):
    raw=bundle_raw()
    if source=="ga4":
        from tests.support.ga4_session_fixtures import session_job
        raw["selections"][0]["ga4"]=session_job().model_dump(mode="json")
    else:
        g=raw["selections"][0]["gsc"]
        g["request"]["before"]=saved_gsc(UUID(raw["case_id"])).model_dump(mode="json")
        g["request"]["after"]=saved_gsc(UUID(raw["case_id"]),after=True).model_dump(mode="json")
        r=ComparisonRequest.model_validate_json(canonical_json_bytes(g["request"]))
        g["result"]=compare_implementation(r).model_dump(mode="json")
    with pytest.raises(DeterministicJobError,match="V22_REVIEW_INVALID"):
        build_review_projection(seal(raw))
