"""Bounded current-plan basis for later rounds; never a fabricated old report.

Pure parsing establishes shape/integrity only. Ownership, completed settlement and
original report equality are checked by the storage resolver before returning it.
"""
from typing import Annotated, Literal
from uuid import UUID
from pydantic import Field, TypeAdapter, model_validator
from app.jobs_v22.digest import canonical_json_bytes, verified_request_digest
from app.report_v22.capability import BusinessScope, CapabilityReport, Checksum, DeliveryRevision, EditableAction, Label, StrictModel, Text, Timestamp

Work = Annotated[str, Field(min_length=1, max_length=4000)]

class PlanAction(EditableAction):
    steps: list[Work] = Field(min_length=1, max_length=12)

    @model_validator(mode='before')
    @classmethod
    def detached(cls,value):
        return value.model_dump(mode='json') if hasattr(value,'model_dump') else value

class PlanBasisFinding(StrictModel):
    classification: Literal['fact','inference','estimate']
    target: str
    observed_fact: str
    observed_at: Timestamp
    comparison_scope: str
    judgment: str
    interpretation: Literal['supported','needs_verification','conflicting']
    limitations: list[str]

class PlanBasisDiagnosis(StrictModel):
    source: Literal['prior_report_not_rechecked']
    title: str
    summary: str
    action_state: Literal['actions_supported','insufficient_basis']
    findings: list[PlanBasisFinding] = Field(max_length=50)
    limitations: list[str] = Field(max_length=40)

class PlanBasisReport(StrictModel):
    schema_version: Literal['v22_review_plan_basis_v1']
    source_schema_version: Literal['2.2.3']
    id: UUID
    report_type: Literal['verified_execution']
    version: int = Field(ge=2,lt=2147483647)
    generated_at: Timestamp
    business: BusinessScope
    title: Label
    summary: Text
    action_state: Literal['actions_supported','no_next_actions']
    actions: list[PlanAction] = Field(max_length=3)
    limitations: list[Text] = Field(min_length=1,max_length=40)
    prior_diagnosis: PlanBasisDiagnosis
    source_checksum: Checksum
    checksum: Checksum

    @model_validator(mode='after')
    def integrity(self):
        if (len({a.id for a in self.actions})!=len(self.actions) or bool(self.actions)!=(self.action_state=='actions_supported')
            or self.checksum!=verified_request_digest(self.model_dump(mode='json',exclude={'checksum'}))):
            raise ValueError('invalid bounded plan basis')
        return self

class PlanBasisDelivery(DeliveryRevision):
    schema_version: Literal['v22_review_delivery_revision_v1']
    actions: list[PlanAction] = Field(max_length=3)

    def validate_report(self, report):
        report=parse_plan_report(report)
        if not isinstance(report,PlanBasisReport): raise ValueError('review delivery requires a review plan basis')
        if (self.report_id,self.user_id,self.case_id,self.source_checksum)!=(report.id,report.business.user_id,report.business.case_id,report.source_checksum):
            raise ValueError('delivery source binding mismatch')
        if [a.id for a in self.actions]!=[a.id for a in report.actions]: raise ValueError('delivery must preserve action order')
        if any(a.materials!=b.materials for a,b in zip(self.actions,report.actions,strict=True)):
            raise ValueError('delivery must preserve materials')
        return self

PlanReport = CapabilityReport | PlanBasisReport
PlanDelivery = DeliveryRevision | PlanBasisDelivery

def parse_plan_report(value):
    raw=value.model_dump(mode='json') if hasattr(value,'model_dump') else value
    return TypeAdapter(PlanReport).validate_json(canonical_json_bytes(raw))

def parse_plan_delivery(value):
    raw=value.model_dump(mode='json') if hasattr(value,'model_dump') else value
    return TypeAdapter(PlanDelivery).validate_json(canonical_json_bytes(raw))

def plan_business(report):
    return report.business if isinstance(report,PlanBasisReport) else report.input_manifest.business

def plan_report_checksum(report):
    return report.source_checksum if isinstance(report,PlanBasisReport) else verified_request_digest(report.model_dump(mode='json'))

def review_plan_basis(report):
    # Full semantic report validation remains at the completed-report repository.
    # Do not include its generation input or private SEO decisions in the basis.
    if report.schema_version!='2.2.3': raise ValueError('completed review report required')
    raw=report.model_dump(mode='json',include={'id','report_type','version','generated_at','business','title','summary','action_state','actions','limitations'})
    raw.update(schema_version='v22_review_plan_basis_v1',source_schema_version='2.2.3',
        prior_diagnosis=report.content.prior_diagnosis.model_dump(mode='json'),source_checksum=verified_request_digest(report.model_dump(mode='json')))
    return PlanBasisReport.model_validate_json(canonical_json_bytes(raw|{'checksum':verified_request_digest(raw)}))
