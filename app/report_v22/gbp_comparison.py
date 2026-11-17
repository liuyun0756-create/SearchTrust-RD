"""V15 saved GBP comparison. Pure projection; no collection path or clock."""
from copy import deepcopy
from datetime import datetime, timedelta
from typing import Literal

from pydantic import ConfigDict, Field, model_validator

from app.jobs_v22.digest import canonical_json_bytes, verified_request_digest
from app.report_v22.models import StrictModel

RULESET = 'v22_capability_public_v15'
METRICS = ('categories', 'rating', 'review_count', 'reviews_90d', 'latest_review', 'visible_replies')


class GbpMetric(StrictModel):
    model_config = ConfigDict(str_strip_whitespace=False)
    status: Literal['observed', 'not_checked', 'not_applicable']
    value: list[str] | float | str | None = None
    lower_bound: bool = False
    numerator: int | None = Field(default=None, ge=0, le=30)
    denominator: int | None = Field(default=None, ge=1, le=30)
    reason: Literal['saved_value', 'customer_reviews_not_collected', 'source_unavailable', 'identity_unresolved', 'date_unavailable', 'no_review_sample', 'no_reviews', 'sample_lower_bound']
    sampled_at: str | None = Field(default=None, max_length=64)
    source_refs: list[str] = Field(max_length=100)

    @model_validator(mode='after')
    def coherent(self):
        if self.sampled_at is not None and datetime.fromisoformat(self.sampled_at.replace('Z','+00:00')).tzinfo is None:
            raise ValueError('sample timestamp needs timezone')
        if (self.status == 'observed') != (self.value is not None):
            raise ValueError('unknown GBP values must stay unknown')
        if self.denominator is not None and (self.numerator is None or self.numerator > self.denominator):
            raise ValueError('invalid sampled reply denominator')
        if self.status != 'observed' and (self.lower_bound or self.numerator is not None or self.denominator is not None):
            raise ValueError('unknown samples must not become zero')
        return self


class GbpComparisonRow(StrictModel):
    model_config = ConfigDict(str_strip_whitespace=False)
    subject: Literal['customer', 'competitor']
    name: str = Field(min_length=1, max_length=240)
    metrics: dict[str, GbpMetric]

    @model_validator(mode='after')
    def metric_keys(self):
        if set(self.metrics) != set(METRICS):
            raise ValueError('GBP metrics required')
        return self


class GbpDifference(StrictModel):
    metric: Literal['categories', 'rating', 'review_count', 'reviews_90d', 'latest_review', 'visible_replies']
    status: Literal['observed', 'not_checked']
    direction: Literal['higher', 'lower', 'different_categories'] | None = None
    competitor_indices: list[int] = Field(max_length=3)
    reason: Literal['same_direction', 'customer_unchecked', 'insufficient_comparators', 'comparison_time_gap']


class GbpComparison(StrictModel):
    schema_version: Literal['gbp_comparison_v1'] = 'gbp_comparison_v1'
    rows: list[GbpComparisonRow] = Field(min_length=1, max_length=4)
    differences: list[GbpDifference] = Field(min_length=6, max_length=6)

    @model_validator(mode='after')
    def validate_rows(self):
        if self.rows[0].subject != 'customer' or any(r.subject != 'competitor' for r in self.rows[1:]):
            raise ValueError('customer must precede confirmed competitors')
        if self.differences != compare_rows(self.rows):
            raise ValueError('GBP comparison gates do not match saved metrics')
        return self


def compare_rows(rows):
    output = []
    for key in METRICS:
        client = rows[0].metrics[key]
        reason, direction, indices = 'customer_unchecked', None, []
        if client.status == 'observed' and client.sampled_at is not None:
            eligible = [(i, r.metrics[key]) for i,r in enumerate(rows[1:], 1)
                        if r.metrics[key].status == 'observed' and r.metrics[key].sampled_at is not None]
            reason = 'insufficient_comparators'
            # Check the time window on the actual compared set, independently per metric.
            for candidate_direction in ('higher', 'lower', 'different_categories'):
                supporters=[]
                for i, item in eligible:
                    if client.lower_bound or item.lower_bound:
                        continue  # overlapping/open-ended bounds cannot establish an ordering
                    if key == 'categories':
                        matches = candidate_direction == 'different_categories' and bool(set(item.value)-set(client.value))
                    elif key in ('rating', 'review_count'):
                        matches = ((candidate_direction == 'higher' and item.value > client.value)
                                   or (candidate_direction == 'lower' and item.value < client.value))
                    else:
                        matches = False  # Phase 1 never compares customer review samples.
                    if matches:
                        supporters.append((i,item))
                if len(supporters) < 2:
                    continue
                # A common category must be observed at >=2 competitors, not unrelated additions.
                if key == 'categories':
                    additions = set().union(*(set(v.value)-set(client.value) for _,v in supporters))
                    supporters = [(i,v) for i,v in supporters if any(x in v.value and sum(x in w.value for _,w in supporters)>=2 for x in additions)]
                    if len(supporters)<2: continue
                times = [datetime.fromisoformat(t.replace("Z","+00:00")) for t in [client.sampled_at, *(v.sampled_at for _,v in supporters)]]
                if max(times)-min(times) > timedelta(hours=24):
                    reason = 'comparison_time_gap'
                    continue
                direction, indices, reason = candidate_direction, [i for i,_ in supporters], 'same_direction'
                break
        output.append(GbpDifference(metric=key, status='observed' if direction else 'not_checked',
            direction=direction, competitor_indices=indices, reason=reason))
    return output


def prepare_comparison_input(manifest, request):
    from app.report_v22.capability import AnalysisInputManifest
    from app.report_v22.keyword_visibility import prepare_keyword_input
    if manifest.ruleset_version != 'v22_capability_public_v14':
        manifest, request = prepare_keyword_input(manifest, request)
    raw = deepcopy(manifest.model_dump(mode='json'))
    raw.update(schema_version='v22_analysis_input_v5', ruleset_version=RULESET)
    raw['input_checksum'] = verified_request_digest({k:v for k,v in raw.items() if k!='input_checksum'})
    return AnalysisInputManifest.model_validate_json(canonical_json_bytes(raw)), request


def _unknown(reason, refs):
    return GbpMetric(status='not_checked', reason=reason, source_refs=refs)


def _observed(value, at, refs, **extra):
    return GbpMetric(status='observed',value=value,sampled_at=at.isoformat() if at else None,source_refs=refs,reason='saved_value',**extra)


def _original_profile_time(profile, serp):
    # A profile copied from a market snapshot is stamped again by the collector.
    # Match it back to the saved originating record/call; never use that restamp.
    if profile.request_record_id:
        return profile.collected_at
    if serp:
        for run in serp.payload.query_runs:
            for record in run.results:
                keys = ('provider_cid', 'provider_data_id', 'provider_place_id')
                matched = any(getattr(record,k) and getattr(record,k)==getattr(profile,k) for k in keys)
                if matched and all(getattr(record,k)==getattr(profile,k) for k in ('rating','review_count','categories')):
                    call = run.maps_call if record.result_type=='maps' else run.google_call
                    if call.status=='succeeded' and not call.checkpoint_hit:
                        return call.completed_at
    return None


def build_gbp_comparison(request):
    from app.report_v22.evidence_bindings import host
    sources={s.kind:s for s in request.evidence_input.sources}
    customer=sources.get('public_gbp'); competitor=sources.get('competitor');serp=sources.get('serp')
    from app.report_v22.business_identity_v2 import BusinessIdentityV2
    from app.report_v22.keyword_visibility import _profile
    confirmed_customer = (_profile(request) if isinstance(request.business_identity, BusinessIdentityV2) else None)
    def refs(source, path):return [f'{source.binding.snapshot_id}:{path}'] if source else []
    customer_metrics={}
    ready=(customer is not None and customer.binding.identity_match_status=='matched'
           and customer.payload.collection_status=='succeeded' and confirmed_customer is not None)
    for key in METRICS:
        path='/payload/record/fields/'+key
        source_refs=refs(customer,path)
        if key in METRICS[3:]:
            metric=_unknown('customer_reviews_not_collected',[])
        else:
            field=getattr(customer.payload.record.fields,key,None) if ready else None
            metric=(_observed(field.value,customer.binding.fetched_at,source_refs)
                    if field is not None and field.state=='observed' and field.value is not None
                    else _unknown('identity_unresolved' if confirmed_customer is None else 'source_unavailable',source_refs))
        customer_metrics[key]=metric
    rows=[GbpComparisonRow(subject='customer',name=request.business_identity.business_name,metrics=customer_metrics)]
    for i,item in enumerate(competitor.payload.competitors if competitor else []):
        prefix=f'/payload/competitors/{i}'; profile=item.public_gbp
        identity=bool(competitor.binding.identity_match_status=='matched' and profile and ((profile.website_url and host(profile.website_url)==host(item.competitor.website_url))
            or any(getattr(profile,'provider_'+k) and getattr(profile,'provider_'+k)==getattr(item.competitor,'provider_'+k,None)
                   for k in ('cid','data_id','place_id'))))
        at=_original_profile_time(profile,serp) if identity else None
        metrics={}
        for key in METRICS[:3]:
            value=getattr(profile,key,None) if profile and item.public_gbp_status!='unavailable' and identity else None
            path=refs(competitor,prefix+'/public_gbp/'+key)
            metrics[key]=(_observed(value,at,path) if value is not None and (key!='categories' or value)
                          else _unknown('source_unavailable' if identity else 'identity_unresolved',path))
        rr=refs(competitor,prefix+'/reviews')
        reviews=list({r.provider_review_id or r.review_record_id:r for r in item.reviews}.values())
        if not identity or item.reviews_status=='unavailable' or not reviews:
            for key in METRICS[3:]:metrics[key]=_unknown('no_review_sample' if identity else 'identity_unresolved',rr)
            if identity and profile.review_count==0 and not reviews:
                metrics['reviews_90d']=_observed(0,at,rr)
                for key in ('latest_review','visible_replies'):metrics[key]=GbpMetric(status='not_applicable',reason='no_reviews',source_refs=rr)
        else:
            t=min(r.collected_at for r in reviews)
            metrics['visible_replies']=_observed(sum(bool(r.owner_response_text) for r in reviews)/len(reviews),t,rr,
                numerator=sum(bool(r.owner_response_text) for r in reviews),denominator=len(reviews))
            if any(r.iso_date is None or r.iso_date>r.collected_at for r in reviews):
                for key in ('reviews_90d','latest_review'):metrics[key]=_unknown('date_unavailable',rr)
            else:
                count=sum(t-timedelta(days=90)<=r.iso_date<=t for r in reviews)
                metrics['reviews_90d']=GbpMetric(status='observed',value=count,lower_bound=True,reason='sample_lower_bound',sampled_at=t.isoformat(),source_refs=rr)
                metrics['latest_review']=_observed(max(r.iso_date for r in reviews).isoformat(),t,rr)
        rows.append(GbpComparisonRow(subject='competitor',name=item.competitor.business_name,metrics=metrics))
    return GbpComparison(rows=rows,differences=compare_rows(rows))
