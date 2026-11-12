"""First-page keyword observations, isolated from historical rules and providers."""
from collections import Counter
from copy import deepcopy
from unittest.mock import Mock, patch

import pytest

from app.jobs_v22.digest import canonical_json_bytes, request_digest, verified_request_digest
from app.report_v22.capability import AnalysisInputManifest, CapabilityReport
from app.report_v22.capability_public import build_public_capability_report
from app.report_v22.keyword_visibility import (KeywordPosition, KeywordRow, assessment,
    build_keyword_visibility, prepare_keyword_input)
from app.collectors.serp_market_models import SerpResultCount
from findings_helpers import seal
from test_p1_05_identity import bound


def keyword_input(states=(True, False, True)):
    m, q = bound()
    source = next(s for s in q.evidence_input.sources if s.kind == "serp")
    gbp = next(s for s in q.evidence_input.sources if s.kind == "public_gbp")
    keys = {k.kind:k.value for k in gbp.payload.record.observed_entity_keys}
    host = q.business_identity.normalized_domain
    for run, state in zip(source.payload.query_runs, states):
        template = run.results[0]
        run.results = []
        for channel in ("maps", "local_pack", "organic"):
            call = run.maps_call if channel == "maps" else run.google_call
            domain = host if state is True else "other.test"
            run.results.append(template.model_copy(update=dict(record_id=request_digest([run.query,channel]),
                call_id=call.call_id, result_type=channel, query=run.query,
                display_name=run.query+" Austin", snippet=run.query+" Austin", position=2,
                rank_source="provider_position", url=f"https://{domain}/", normalized_domain=domain,
                provider_cid=keys.get("cid") if state is True else "99999999",
                provider_place_id=keys.get("place_id") if state is True else "other-place",
                provider_data_id=keys.get("data_id") if state is True else None)))
        run.limitations = []
        if state is None:
            run.results = [r for r in run.results if r.result_type == "maps"]
    counts = Counter(r.result_type for run in source.payload.query_runs for r in run.results)
    source.payload.complete_query_count = sum(r.complete for r in source.payload.query_runs)
    source.payload.result_counts = [SerpResultCount(result_type=k,count=counts[k]) for k in ("maps","local_pack","organic")]
    source = seal(source)
    for i,s in enumerate(q.evidence_input.sources):
        if s.kind == "serp":q.evidence_input.sources[i] = source
        if s.kind == "competitor":
            s.payload.market_snapshot_checksum = source.binding.payload_checksum
            q.evidence_input.sources[i] = seal(s)
    return reseal(m,q)


def reseal(m,q):
    raw = deepcopy(m.model_dump(mode="json"))
    for ref in raw["snapshots"]:
        ref["payload_checksum"] = str(next(s.binding.payload_checksum for s in q.evidence_input.sources if s.kind == ref["source_kind"]))
    raw["generation_input_checksum"] = verified_request_digest(q.model_dump(mode="json"))
    raw["input_checksum"] = verified_request_digest({k:v for k,v in raw.items() if k != "input_checksum"})
    return prepare_keyword_input(AnalysisInputManifest.model_validate_json(canonical_json_bytes(raw)),q)


def test_first_page_observed_absent_and_failed_are_distinct_and_short_page_counts():
    m,q = keyword_input((True,False,None))
    report = build_public_capability_report(m,q)
    module = report.keyword_visibility
    assert report.schema_version == "2.2.9" and m.schema_version == "v22_analysis_input_v4"
    assert [r.query for r in module.rows] == q.evidence_input.context.queries
    for channel in ("organic","local_pack"):
        assert [getattr(r,channel).status for r in module.rows] == ["observed","not_detected","not_checked"]
        assert getattr(module.rows[0],channel).position == 2
        assert getattr(module.rows[1],channel).position is None
        assert getattr(module,channel).checked_keywords == 2
        assert getattr(module,channel).assessment == "medium"


@pytest.mark.parametrize("states,expected,k,h",[
    ((None,None,None),"not_assessed",0,0), ((True,None,None),"not_assessed",1,1),
    ((True,True,None),"strong",2,2), ((False,False,None),"weak",2,0),
    ((True,False,None),"medium",2,1), ((True,True,False),"strong",3,2),
    ((True,False,False),"medium",3,1), ((False,False,False),"weak",3,0),
])
def test_keyword_thresholds_unknowns_and_exact_thirds(states,expected,k,h):
    m,q = keyword_input(states)
    module = build_keyword_visibility(q)
    for channel in ("organic","local_pack"):
        a = getattr(module,channel)
        assert (a.assessment,a.checked_keywords,a.observed_keywords,a.total_keywords)==(expected,k,h,3)
        assert a.ratio == (h/k if k>=2 else None)


def test_maps_never_supplies_a_missing_local_pack_and_missing_url_can_match_saved_branch():
    m,q = keyword_input()
    source = next(s for s in q.evidence_input.sources if s.kind == "serp")
    first = source.payload.query_runs[0]
    first.results = [r for r in first.results if r.result_type != "local_pack"]
    second = source.payload.query_runs[2]
    second.results = [r.model_copy(update={"url":None,"normalized_domain":None}) if r.result_type == "local_pack" else r for r in second.results]
    module = build_keyword_visibility(q)
    assert module.rows[0].maps.position == 2
    assert module.rows[0].local_pack.status == "not_checked"
    assert module.rows[2].local_pack.position == 2  # explicit bound branch IDs, no invented URL


def test_unknown_identity_parse_loss_and_nonlocal_samples_cannot_be_absence():
    m,q = keyword_input((False,False,False))
    source = next(s for s in q.evidence_input.sources if s.kind == "serp")
    run = source.payload.query_runs[0]
    run.results = [r.model_copy(update={"url":None,"normalized_domain":None}) if r.result_type == "organic" else r for r in run.results]
    source.payload.query_runs[1].limitations = ["SERP_ORGANIC_RESULT_SKIPPED"]
    source.payload.query_runs[2].results = [r.model_copy(update={"display_name":"Plumbing Boise, ID", "snippet":"Located in Boise, ID"}) if r.result_type == "organic" else r for r in source.payload.query_runs[2].results]
    module = build_keyword_visibility(q)
    assert [r.organic.reason for r in module.rows] == ["identity_unresolved","parse_incomplete","sample_not_local"]
    assert module.organic.assessment == "not_assessed"


def test_other_branch_with_same_domain_does_not_match_and_name_is_not_identity():
    m,q = keyword_input((False,False,False))
    for run in next(s for s in q.evidence_input.sources if s.kind == "serp").payload.query_runs:
        for r in run.results:
            if r.result_type == "local_pack":
                r.normalized_domain = q.business_identity.normalized_domain
                r.url = q.business_identity.site_url
                r.display_name = q.business_identity.business_name
    module = build_keyword_visibility(q)
    assert all(r.local_pack.status == "not_detected" for r in module.rows)


def test_observation_uses_saved_position_not_text_and_preserves_input_bytes(monkeypatch):
    m,q = keyword_input()
    before = canonical_json_bytes(q)
    with patch("httpx.AsyncClient.send", side_effect=AssertionError("NO_PROVIDER_CALL_ALLOWED")) as send:
        first = build_public_capability_report(m,q)
        second = build_public_capability_report(m,q)
    send.assert_not_called()
    assert canonical_json_bytes(first)==canonical_json_bytes(second)
    assert canonical_json_bytes(q)==before
    assert first.keyword_visibility.rows[0].organic.position==2
    bad = first.model_dump(mode="json")
    bad["keyword_visibility"]["organic"]["checked_keywords"]=0
    with pytest.raises(ValueError):CapabilityReport.model_validate(bad)


def test_version_gate_rejects_unbound_queries_and_module_on_historical_report():
    m,q = keyword_input()
    before = build_public_capability_report(m,q)
    raw = before.model_dump(mode="json")
    raw["input_manifest"]["keyword_queries"][0]="wrong query"
    with pytest.raises(ValueError):CapabilityReport.model_validate(raw)
    old_m,old_q=bound()
    old=build_public_capability_report(old_m,old_q).model_dump(mode="json")
    assert "keyword_visibility" not in old and "keyword_queries" not in old["input_manifest"]
    old["keyword_visibility"]=before.keyword_visibility.model_dump(mode="json")
    with pytest.raises(ValueError):CapabilityReport.model_validate(old)


import hashlib,json
from pathlib import Path
_HISTORY=json.loads((Path(__file__).parent/'fixtures/p1-01-history.json').read_text())
@pytest.mark.parametrize('row',_HISTORY['reports'],ids=lambda r:f"v{r['version']}-{r['sample']}")
def test_v12_v13_bytes_match_unmodified_commit_before_p1_01(row):
    if row['version']==12:
        from test_zero_action_presentation import historical
        _,_,report=historical(12,row['sample'])
    else:
        from app.report_v22.identity_input import prepare_identity_input
        from test_p1_05_identity import confirmation
        m,q=bound();m,q,_,_=prepare_identity_input(m,q,confirmation=confirmation(q,row['sample']))
        report=build_public_capability_report(m,q)
    raw=canonical_json_bytes(report)
    assert len(raw)==row['bytes']
    assert hashlib.sha256(raw).hexdigest()==row['sha256']
    assert 'keyword_visibility' not in report.model_dump(mode='json')


def test_failed_call_unknown_branch_and_ineligible_source_never_become_absence():
    m,q=keyword_input()
    source=next(s for s in q.evidence_input.sources if s.kind=='serp')
    source.payload.query_runs[0].google_call.status='failed_transient'
    assert build_keyword_visibility(q).rows[0].organic.reason=='request_failed'
    q.business_identity.identity_match.state='unresolved'
    assert all(r.local_pack.status=='not_checked' for r in build_keyword_visibility(q).rows)
    source.binding.health_status='unavailable'
    assert all(r.organic.reason=='source_unavailable' for r in build_keyword_visibility(q).rows)


def test_keyword_generation_performs_zero_provider_calls_and_preserves_action_decisions():
    from app.report_v22.identity_input import prepare_identity_input
    m,q=bound();identity_m,identity_q,_,_=prepare_identity_input(m,q)
    old=build_public_capability_report(identity_m,identity_q)
    m,q=prepare_keyword_input(identity_m,identity_q)
    with patch('httpx.Client.send',side_effect=AssertionError('NO_PROVIDER_CALL_ALLOWED')) as sync_send, patch('httpx.AsyncClient.send',side_effect=AssertionError('NO_PROVIDER_CALL_ALLOWED')) as async_send:
        report=build_public_capability_report(m,q)
    sync_send.assert_not_called();async_send.assert_not_called()
    assert report.actions==old.actions and report.action_state==old.action_state
    assert report.findings==old.findings and report.evidence==old.evidence


def test_feature_flag_is_default_off_and_does_not_touch_collected_inputs():
    from app.core.config import Settings
    from app.jobs_v22.capability_acquisition_preparation import CapabilityAcquisitionPreparer
    assert Settings.model_fields['V22_TARGET_KEYWORDS_ENABLED'].default is False
    preparer=CapabilityAcquisitionPreparer(repository=Mock(),collector=Mock(),discovery_verifier=Mock())
    assert preparer.target_keywords_enabled is False


def test_five_keyword_assessment_preserves_order_and_counts_only_known_channel_rows():
    from app.report_v22.keyword_visibility import KeywordVisibility
    _,q=keyword_input((True,False,None))
    module=build_keyword_visibility(q)
    rows=[module.rows[0].model_copy(update={'query':query}) for query in ['first','second','third','fourth','fifth']]
    rows[1]=module.rows[1].model_copy(update={'query':'second'})
    rows[4]=module.rows[2].model_copy(update={'query':'fifth'})
    result=KeywordVisibility(rows=rows,local_pack=assessment(rows,'local_pack'),organic=assessment(rows,'organic'),sample=module.sample)
    assert [r.query for r in result.rows]==['first','second','third','fourth','fifth']
    assert result.organic.total_keywords==5 and result.organic.checked_keywords==4
    assert result.organic.observed_keywords==3 and result.organic.assessment=='strong'


def test_response_order_keeps_its_saved_basis_and_ignores_title_numbers():
    _,q=keyword_input()
    source=next(s for s in q.evidence_input.sources if s.kind=='serp')
    for run in source.payload.query_runs:
        for record in run.results:
            record.rank_source='response_order'
            record.display_name=run.query+' Austin number 999'
    result=build_keyword_visibility(q)
    assert result.rows[0].organic.position==2
    assert result.rows[0].organic.rank_source=='response_order'
    assert result.rows[0].local_pack.rank_source=='response_order'


def test_keyword_positions_do_not_depend_on_the_legacy_card_evidence_budget(monkeypatch):
    import app.report_v22.resource_limits as limits
    m,q=keyword_input()
    full=build_public_capability_report(m,q)
    positions={e.id for e in full.evidence if e.field_path.endswith('/position')}
    assert positions
    original=limits.select_evidence
    def no_positions(ids,source_kind,limit=20):
        selected,omitted=original(ids,source_kind,limit)
        kept=[key for key in selected if key not in positions]
        return kept,omitted+len(selected)-len(kept)
    monkeypatch.setattr(limits,'select_evidence',no_positions)
    report=build_public_capability_report(m,q)
    assert report.keyword_visibility.rows[0].organic.position==2
    assert report.keyword_visibility.rows[0].local_pack.position==2
    assert not any(e.field_path.endswith('/position') for e in report.evidence)


def test_v14_identity_reassessment_retains_keyword_binding_and_requires_new_approval(monkeypatch):
    from uuid import UUID
    from app.report_v22.public_reassessment import reassess_public_report
    from test_p1_05_reassessment import selection
    m,q=keyword_input();base=build_public_capability_report(m,q)
    before=canonical_json_bytes(base)+canonical_json_bytes(q)
    with patch('httpx.Client.send',side_effect=AssertionError('NO_PROVIDER_CALL_ALLOWED')) as sync_send, patch('httpx.AsyncClient.send',side_effect=AssertionError('NO_PROVIDER_CALL_ALLOWED')) as async_send:
        report,updated,diff=reassess_public_report(base,q,selection(q),
            request_id=UUID('11111111-1111-4111-8111-111111111111'),evaluated_at='2026-11-05T12:00:00Z')
    sync_send.assert_not_called();async_send.assert_not_called()
    assert report.schema_version=='2.2.9' and report.input_manifest.schema_version=='v22_analysis_input_v4'
    assert report.input_manifest.ruleset_version=='v22_capability_public_v14'
    assert report.input_manifest.keyword_queries==m.keyword_queries
    assert report.keyword_visibility.rows==base.keyword_visibility.rows
    assert report.keyword_visibility.organic==base.keyword_visibility.organic
    assert report.keyword_visibility.local_pack==base.keyword_visibility.local_pack
    assert report.keyword_visibility.sample.selected_branch_ref==updated.business_identity.selected_branch_ref
    assert canonical_json_bytes(updated.evidence_input)==canonical_json_bytes(q.evidence_input)
    assert canonical_json_bytes(base)+canonical_json_bytes(q)==before
    assert diff['requires_approval'] and not diff['copy_migrated']
