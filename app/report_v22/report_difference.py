"""Bounded, immutable generated-version comparison. Not delivery edits or effects."""
from copy import deepcopy
from typing import Literal
from uuid import UUID
from pydantic import Field, model_validator
from app.jobs_v22.digest import verified_request_digest
from app.report_v22.capability import Checksum, Timestamp
from app.report_v22.comparison_base import StrictModel
from app.report_v22.review_plan_basis import PlanAction, PlanBasisReport, plan_business
from app.report_v22.review_projection_models import ActionFacts
from app.report_v22.review_generation_models import MAX_INPUT, ReviewGenerationInput, checked, invalid
from app.report_v22.review_report import MAX_REPORT, VerifiedReviewReport, validate_verified_review_report

# At most one current report (1.3 MB), prior facts (<=1.3 MB), and
# approved work from the bounded 2.3 MB input. Never recursive history.
MAX_DIFFERENCE=5_000_000
WORK_FIELDS=('title','target','steps','completion','review','materials')
EVIDENCE_SECTIONS=('recorded_work','page','gsc','ga4','checks')

def work_fields(action):
    if action is None:return None
    raw=action.model_dump(mode='json') if hasattr(action,'model_dump') else deepcopy(action)
    raw={k:raw[k] for k in WORK_FIELDS}
    raw['materials']=[{k:v for k,v in m.items() if k not in ('id','evidence_ids')} for m in raw['materials']]
    return raw

def evidence_sections(facts):
    f=facts.model_dump(mode='json') if hasattr(facts,'model_dump') else deepcopy(facts)
    f={k:f[k] for k in EVIDENCE_SECTIONS}
    omitted={'page':('observed_at','previous_observed_at','comparison','comparison_reason','changes'),
             'gsc':('evaluated_at',),'ga4':('started_at','completed_at')}
    for section,keys in omitted.items():
        if f[section] is not None:
            f[section]={k:v for k,v in f[section].items() if k not in keys}
    return f

class DifferenceBasis(StrictModel):
    report_id:UUID
    report_checksum:Checksum
    report_version:int=Field(ge=1,lt=2147483647)
    delivery_id:UUID
    delivery_revision:int=Field(ge=1,lt=2147483647)
    delivery_checksum:Checksum

class EvidenceDifference(StrictModel):
    action_id:str=Field(min_length=1,max_length=200)
    state:Literal['first_baseline','compared','not_comparable']
    reason:Literal['no_previous_review','no_previous_action_observation','different_business_scope','different_target']|None
    before:ActionFacts|None
    after:ActionFacts
    changed_sections:list[Literal['recorded_work','page','gsc','ga4','checks']]=Field(max_length=5)

    @model_validator(mode='after')
    def consistency(self):
        if self.after.action_id!=self.action_id or (self.before and self.before.action_id!=self.action_id):raise ValueError('action mismatch')
        if self.state=='compared':
            if self.reason is not None or self.before is None or self.before.target!=self.after.target:raise ValueError('invalid evidence comparison')
            a,b=evidence_sections(self.before),evidence_sections(self.after)
            expected=[k for k in EVIDENCE_SECTIONS if a[k]!=b[k]]
            if self.changed_sections!=expected:raise ValueError('false evidence changes')
        elif self.changed_sections or self.reason is None:raise ValueError('invalid evidence baseline')
        if self.state=='first_baseline' and (self.before is not None or self.reason not in ('no_previous_review','no_previous_action_observation')):raise ValueError('invalid first baseline')
        if self.state=='not_comparable' and (self.reason not in ('different_business_scope','different_target') or self.before is None):raise ValueError('invalid incomparable basis')
        return self

class WorkDifference(StrictModel):
    action_id:str=Field(min_length=1,max_length=200)
    state:Literal['added','updated','unchanged','not_carried_forward']
    before:PlanAction|None
    after:PlanAction|None
    changed_fields:list[Literal['title','target','steps','completion','review','materials']]=Field(max_length=6)

    @model_validator(mode='after')
    def consistency(self):
        if (not self.before and not self.after) or any(a.id!=self.action_id for a in (self.before,self.after) if a):raise ValueError('invalid work identity')
        if not self.before:state,fields='added',[]
        elif not self.after:state,fields='not_carried_forward',[]
        else:
            a,b=work_fields(self.before),work_fields(self.after)
            fields=[k for k in WORK_FIELDS if a[k]!=b[k]]
            state='updated' if fields else 'unchanged'
        if self.state!=state or self.changed_fields!=fields:raise ValueError('false work changes')
        return self

class ReportDifference(StrictModel):
    schema_version:Literal['v22_review_report_difference_v1']
    rules_version:Literal['v22_report_difference_rules_v1']
    user_id:UUID
    case_id:UUID
    report_id:UUID
    report_checksum:Checksum
    report_version:int=Field(ge=2,lt=2147483647)
    generated_at:Timestamp
    input_checksum:Checksum
    basis:DifferenceBasis
    evidence_basis:Literal['first_review','previous_review']
    evidence:list[EvidenceDifference]=Field(max_length=3)
    work:list[WorkDifference]=Field(max_length=6)
    basis_action_order:list[str]=Field(max_length=3)
    current_action_order:list[str]=Field(max_length=3)
    plan_order_changed:bool
    checksum:Checksum

    @model_validator(mode='after')
    def consistency(self):
        if self.report_id==self.basis.report_id or self.report_version!=self.basis.report_version+1:raise ValueError('invalid version basis')
        if len({a.action_id for a in self.evidence})!=len(self.evidence) or len({a.action_id for a in self.work})!=len(self.work):raise ValueError('duplicate actions')
        if self.evidence_basis=='first_review' and any(a.state!='first_baseline' or a.reason!='no_previous_review' for a in self.evidence):raise ValueError('false baseline')
        old,new=self.basis_action_order,self.current_action_order
        if len(set(old))!=len(old) or len(set(new))!=len(new) or set(old)!={a.action_id for a in self.work if a.before} or set(new)!={a.action_id for a in self.work if a.after}:raise ValueError('invalid plan order')
        if [a.action_id for a in self.evidence]!=old or [a.action_id for a in self.work]!=[*new,*(k for k in old if k not in new)]:raise ValueError('invalid action sequence')
        if self.plan_order_changed!=([k for k in old if k in new]!=[k for k in new if k in old]):raise ValueError('false order change')
        if self.checksum!=verified_request_digest(self.model_dump(mode='json',exclude={'checksum'})):raise ValueError('difference checksum mismatch')
        return self

def _business_scope(b):
    return b.site_url,b.primary_service,b.target_market

def build_report_difference(value,report,previous=None):
    value=checked(ReviewGenerationInput,value,MAX_INPUT)
    report=validate_verified_review_report(report,value)
    basis=value.evidence.bundle
    later=isinstance(basis.report,PlanBasisReport)
    if later:
        if previous is None:raise invalid('DIFFERENCE_BASIS_MISSING')
        previous=checked(VerifiedReviewReport,previous,MAX_REPORT)
        if (previous.id,previous.version,previous.business.user_id,previous.business.case_id,verified_request_digest(previous.model_dump(mode='json')))!=(basis.report.id,basis.report.version,value.user_id,value.case_id,basis.report_checksum):raise invalid('DIFFERENCE_BASIS_INVALID')
    elif previous is not None:raise invalid('DIFFERENCE_BASIS_INVALID')
    previous_facts={a.facts.action_id:a.facts for a in previous.content.seo_actions} if previous else {}
    evidence=[]
    for action in report.content.seo_actions:
        after=action.facts;before=previous_facts.get(after.action_id)
        state,reason='compared',None
        if not later:state,reason='first_baseline','no_previous_review'
        elif before is None:state,reason='first_baseline','no_previous_action_observation'
        elif _business_scope(previous.business)!=_business_scope(plan_business(basis.report)):state,reason='not_comparable','different_business_scope'
        elif before.target!=after.target:state,reason='not_comparable','different_target'
        fields=[]
        if state=='compared':
            a,b=evidence_sections(before),evidence_sections(after)
            fields=[k for k in EVIDENCE_SECTIONS if a[k]!=b[k]]
        evidence.append(dict(action_id=after.action_id,state=state,reason=reason,before=before.model_dump(mode='json') if before else None,after=after.model_dump(mode='json'),changed_sections=fields))
    old={a.id:a for a in basis.delivery.actions};new={a.id:a for a in report.actions}
    work=[]
    for identifier in [*new,*(k for k in old if k not in new)]:
        a,b=old.get(identifier),new.get(identifier);fields=[]
        if a and b:
            left,right=work_fields(a),work_fields(b);fields=[k for k in WORK_FIELDS if left[k]!=right[k]]
        state='added' if not a else 'not_carried_forward' if not b else 'updated' if fields else 'unchanged'
        work.append(dict(action_id=identifier,state=state,before=a.model_dump(mode='json') if a else None,after=b.model_dump(mode='json') if b else None,changed_fields=fields))
    raw=dict(schema_version='v22_review_report_difference_v1',rules_version='v22_report_difference_rules_v1',user_id=str(value.user_id),case_id=str(value.case_id),report_id=str(report.id),report_checksum=verified_request_digest(report.model_dump(mode='json')),report_version=report.version,generated_at=report.generated_at,input_checksum=value.checksum,
        basis=dict(report_id=str(basis.report.id),report_checksum=basis.report_checksum,report_version=basis.report.version,delivery_id=str(basis.delivery_id),delivery_revision=basis.delivery_revision,delivery_checksum=basis.delivery_checksum),
        evidence_basis='previous_review' if later else 'first_review',evidence=evidence,work=work,basis_action_order=list(old),current_action_order=list(new),plan_order_changed=[k for k in old if k in new]!=[k for k in new if k in old])
    return checked(ReportDifference,raw|dict(checksum=verified_request_digest(raw)),MAX_DIFFERENCE)
