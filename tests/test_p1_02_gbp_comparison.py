"""V15 comparison is derived solely from bound, saved observations."""
from datetime import timedelta
from copy import deepcopy
from unittest.mock import patch

import pytest
from app.competitors_v22.models import PublicGbpProfile, PublicReviewRecord
from app.jobs_v22.digest import canonical_json_bytes, request_digest
from app.report_v22.gbp_comparison import (build_gbp_comparison, prepare_comparison_input, compare_rows, GbpMetric)
from app.report_v22.capability_public import build_public_capability_report
from app.report_v22.capability import CapabilityReport
from test_p1_01_keyword_visibility import keyword_input, reseal
from findings_helpers import seal


def comparison_input():
    m,q=keyword_input()
    customer=next(s for s in q.evidence_input.sources if s.kind=='public_gbp')
    from app.report_v22.public_gbp_models import CustomerPublicGbpSnapshotV2
    raw=customer.payload.model_dump(mode='json');raw['schema_version']='customer_public_gbp_snapshot_v2'
    raw['record']['fields'].update(categories={'state':'observed','value':['Plumber']},rating={'state':'observed','value':4.2},review_count={'state':'observed','value':117},
        **{k:{'state':'not_returned','value':None} for k in ('hours','operating_hours','photo_count')})
    customer.payload=CustomerPublicGbpSnapshotV2.model_validate_json(canonical_json_bytes(raw))
    customer.binding.schema_version='customer_public_gbp_snapshot_v2'
    q.evidence_input.sources[q.evidence_input.sources.index(customer)]=seal(customer)
    next(s for s in m.snapshots if s.source_kind=='public_gbp').schema_version='customer_public_gbp_snapshot_v2'
    source=next(s for s in q.evidence_input.sources if s.kind=='competitor')
    now=q.evidence_input.context.evaluated_at
    for i,item in enumerate(source.payload.competitors):
        item.public_gbp=PublicGbpProfile(business_name=item.competitor.business_name,website_url=item.competitor.website_url,
            categories=['Plumber'],rating=4.7+i/10,review_count=100+i,collected_at=customer.binding.fetched_at,request_record_id='req_'+'a'*24)
        item.public_gbp_status='available';item.reviews_status='available'
        item.reviews=[PublicReviewRecord(review_record_id='rv_'+str(n).zfill(16),provider_review_id=str(n),rating=5.0,
            iso_date=now-timedelta(days=n),collected_at=now,source='google_maps_reviews',request_record_id='req_'+'b'*24,
            owner_response_text='Thank you' if n<20 else None) for n in range(30)]
    idx=q.evidence_input.sources.index(source);q.evidence_input.sources[idx]=seal(source)
    # Synthetic reseal is separate from the immutable original fixtures.
    from app.report_v22.operating_model import inspect_operating_model
    q.business_identity.operating_model=inspect_operating_model(q)[0]
    m.identity_checksum=request_digest(q.business_identity)
    m,q=reseal(m,q)
    return prepare_comparison_input(m,q)


def test_v15_builds_profiles_lower_bounds_sample_replies_and_unknown_customer_without_provider_calls():
    m,q=comparison_input(); before=canonical_json_bytes(q)
    with patch('httpx.AsyncClient.send',side_effect=AssertionError('NO_PROVIDER_CALL_ALLOWED')) as send:
        report=build_public_capability_report(m,q)
    send.assert_not_called()
    assert canonical_json_bytes(q)==before
    assert report.schema_version=='2.2.10' and m.schema_version=='v22_analysis_input_v5'
    module=report.gbp_comparison
    assert len(module.rows)==4
    assert module.rows[0].metrics['rating'].value==4.2
    assert module.rows[0].metrics['review_count'].value==117
    assert next(d for d in module.differences if d.metric=='rating').status=='observed'
    for row in module.rows[1:]:
        assert row.metrics['reviews_90d'].value==30 and row.metrics['reviews_90d'].lower_bound
        assert (row.metrics['visible_replies'].numerator,row.metrics['visible_replies'].denominator)==(20,30)
    assert all(module.rows[0].metrics[k].reason=='customer_reviews_not_collected' for k in ('reviews_90d','latest_review','visible_replies'))
    assert CapabilityReport.model_validate_json(report.model_dump_json())==report


@pytest.mark.parametrize('case',['cross_window','bad_date','future_date','duplicate','empty','unavailable','zero_reviews'])
def test_sample_boundaries_do_not_turn_collection_or_date_gaps_into_zero(case):
    _,q=comparison_input(); c=next(s for s in q.evidence_input.sources if s.kind=='competitor').payload.competitors[0]
    if case=='cross_window':c.reviews[-1].iso_date-=timedelta(days=100)
    if case=='bad_date':c.reviews[0].iso_date=None
    if case=='future_date':c.reviews[0].iso_date+=timedelta(days=1)
    if case=='duplicate':c.reviews[-1]=deepcopy(c.reviews[0])
    if case in ('empty','zero_reviews'):c.reviews=[];c.reviews_status='unavailable'
    if case=='unavailable':c.reviews_status='unavailable'
    if case=='zero_reviews':c.public_gbp.review_count=0
    metrics=build_gbp_comparison(q).rows[1].metrics
    if case=='cross_window':assert metrics['reviews_90d'].value==29 and metrics['reviews_90d'].lower_bound
    if case in ('bad_date','future_date'):assert metrics['reviews_90d'].status==metrics['latest_review'].status=='not_checked'
    if case=='duplicate':assert metrics['visible_replies'].denominator==29
    if case in ('empty','unavailable'):assert metrics['reviews_90d'].value is None
    if case=='zero_reviews':assert metrics['reviews_90d'].value==0 and metrics['latest_review'].status=='not_applicable'


@pytest.mark.parametrize('hours,comparators,expected',[(24,2,'observed'),(24.0001,2,'not_checked'),(0,1,'not_checked')])
def test_each_metric_requires_two_comparators_and_original_time_window(hours,comparators,expected):
    _,q=comparison_input(); module=build_gbp_comparison(q)
    t=q.evidence_input.context.evaluated_at
    for i,row in enumerate(module.rows):
        row.metrics['rating']=GbpMetric(status='observed',value=3.0 if i==0 else 4.0,reason='saved_value',
            sampled_at=(t+timedelta(hours=hours if i else 0)).isoformat(),source_refs=[])
    rows=module.rows[:comparators+1]
    rating=next(x for x in compare_rows(rows) if x.metric=='rating')
    assert rating.status==expected
    assert next(x for x in compare_rows(rows) if x.metric=='visible_replies').status=='not_checked'


def test_lower_bounds_never_order_and_missing_profile_not_counted_as_zero():
    _,q=comparison_input(); module=build_gbp_comparison(q)
    module.rows[0].metrics['rating'].lower_bound=True
    assert next(x for x in compare_rows(module.rows) if x.metric=='rating').status=='not_checked'
    c=next(s for s in q.evidence_input.sources if s.kind=='competitor').payload.competitors[0]
    c.public_gbp_status='unavailable';c.public_gbp=None
    assert build_gbp_comparison(q).rows[1].metrics['rating'].value is None


def test_gbp_version_gate_and_default_flag_do_not_modify_historical_report():
    from app.core.config import Settings
    from app.report_v22.capability import CapabilityReport
    m,q=keyword_input();old=build_public_capability_report(m,q); before=canonical_json_bytes(old)
    assert 'gbp_comparison' not in old.model_dump(mode='json')
    assert Settings().V22_GBP_COMPARISON_ENABLED is False
    new_m,new_q=prepare_comparison_input(m,q);new=build_public_capability_report(new_m,new_q)
    assert canonical_json_bytes(build_public_capability_report(m,q))==before
    bad=old.model_dump(mode='json');bad['gbp_comparison']=new.gbp_comparison.model_dump(mode='json')
    with pytest.raises(ValueError):CapabilityReport.model_validate_json(canonical_json_bytes(bad))
    assert old.findings==new.findings and old.actions==new.actions


def test_ninety_day_window_is_inclusive_in_utc_and_unknown_time_never_uses_replay_clock():
    from datetime import timezone
    _,q=comparison_input()
    item=next(s for s in q.evidence_input.sources if s.kind=='competitor').payload.competitors[0]
    t=item.reviews[0].collected_at
    item.reviews[-1].iso_date=(t-timedelta(days=90)).astimezone(timezone(timedelta(hours=9)))
    assert build_gbp_comparison(q).rows[1].metrics['reviews_90d'].value==30
    item.reviews[-1].iso_date-=timedelta(microseconds=1)
    assert build_gbp_comparison(q).rows[1].metrics['reviews_90d'].value==29
    # Market copies have a fresh collector timestamp but no original request ID.
    # With no matching original call, this value cannot enter a timed comparison.
    item.public_gbp.request_record_id=None
    module=build_gbp_comparison(q)
    assert module.rows[1].metrics['rating'].status=='observed'
    assert module.rows[1].metrics['rating'].sampled_at is None


def test_category_difference_needs_a_shared_addition_at_two_competitors():
    _,q=comparison_input(); module=build_gbp_comparison(q);rows=module.rows[:3]
    for i,row in enumerate(rows):
        row.metrics['categories']=GbpMetric(status='observed',value=[['A','B','C'][i]],reason='saved_value',
            sampled_at=q.evidence_input.context.evaluated_at.isoformat(),source_refs=[])
    assert compare_rows(rows)[0].status=='not_checked'
    rows[2].metrics['categories'].value=['B']
    assert compare_rows(rows)[0].status=='observed'


def test_customer_identity_requires_the_existing_positive_domain_phone_branch_gate():
    _,q=comparison_input()
    q.business_identity.identity_match.state='not_checked'
    metrics=build_gbp_comparison(q).rows[0].metrics
    assert all(metrics[k].status=='not_checked' and metrics[k].reason=='identity_unresolved' for k in ('categories','rating','review_count'))


def test_saved_category_values_are_not_trimmed_or_rewritten():
    _,q=comparison_input()
    source=next(s for s in q.evidence_input.sources if s.kind=='public_gbp')
    source.payload.record.fields.categories.state='observed'
    source.payload.record.fields.categories.value=['  Plumber  ']
    assert build_gbp_comparison(q).rows[0].metrics['categories'].value==['  Plumber  ']


def test_unresolved_competitor_source_keeps_rows_and_never_claims_zero():
    _,q=comparison_input(); source=next(s for s in q.evidence_input.sources if s.kind=='competitor')
    source.binding.identity_match_status='needs_confirmation'
    module=build_gbp_comparison(q)
    assert len(module.rows)==4
    assert all(row.metrics['rating'].value is None for row in module.rows[1:])
