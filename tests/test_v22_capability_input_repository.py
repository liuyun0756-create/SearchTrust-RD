from copy import deepcopy
import json
from uuid import UUID

import httpx
import pytest

from app.jobs_v22.capability_input_repository import SupabaseCapabilityInputRepository,require_frozen_capability_input,FrozenCapabilityInput
from app.jobs_v22.capability_runtime_models import CapabilityTaskRequest
from app.jobs_v22.digest import canonical_json_bytes,verified_request_digest
from app.jobs_v22.errors import DeterministicJobError,TransientJobError
from app.report_v22.capability_public import build_public_capability_report
from app.report_v22.capability_service import build_service_capability_report
from test_v22_capability_public import inputs as public_inputs
from test_v22_capability_service import inputs as service_inputs


@pytest.fixture
def anyio_backend(): return "asyncio"


def graph(mode="acquisition",completed=False):
    if mode=="acquisition": m,r=public_inputs();parent=None;result=build_public_capability_report(m,r)
    else: m,r,parent=service_inputs(mode=mode);result=build_service_capability_report(m,r,previous_report=parent)
    task=CapabilityTaskRequest(case_id=m.case_id,manifest_id=m.id,input_checksum=m.input_checksum,report_type=result.report_type)
    raw=dict(schema_version="v22_capability_resolved_input_v1",manifest=m.model_dump(mode="json"),run_generation=1,
        request=None if completed else r.model_dump(mode="json"),previous_report=None if completed else parent,
        completed_report=result.model_dump(mode="json") if completed else None)
    return m,r,parent,task,raw


async def resolve(raw,task=None,job=None):
    m,_,_,default,_=graph()
    calls=[]
    def respond(sent): calls.append(sent);return httpx.Response(200,json=raw)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        frozen=await SupabaseCapabilityInputRepository(url="https://storage.example",service_role_key="test-key",http_client=client).resolve(
            job_id=job or m.job_id,request=task or default,run_generation=1)
    return frozen,calls


@pytest.mark.anyio
@pytest.mark.parametrize("mode",["acquisition","first_service","after_acquisition"])
@pytest.mark.parametrize("completed",[False,True])
async def test_resolver_exact_frozen_or_completed_graph_is_sealed(mode,completed):
    m,r,parent,task,raw=graph(mode,completed)
    frozen,calls=await resolve(raw,task,m.job_id)
    assert require_frozen_capability_input(frozen,run_generation=1) is frozen
    assert frozen.manifest==m and (frozen.completed_report is not None)==completed
    assert len(calls)==1 and calls[0].url.path.endswith("/resolve_v22_capability_analysis_input")
    assert json.loads(calls[0].content)==dict(p_job_id=str(m.job_id),p_case_id=str(m.case_id),p_manifest_id=str(m.id),p_input_checksum=m.input_checksum,p_run_generation=1)
    clone=FrozenCapabilityInput(frozen.manifest,frozen.request,frozen.previous_report,frozen.completed_report)
    with pytest.raises(ValueError):require_frozen_capability_input(clone,run_generation=1)
    with pytest.raises(ValueError):require_frozen_capability_input(frozen,run_generation=2)
    frozen.manifest.ruleset_version="mutated"
    with pytest.raises(ValueError):require_frozen_capability_input(frozen,run_generation=1)


@pytest.mark.anyio
@pytest.mark.parametrize("change",["extra","generation","case","manifest","query","source","request-null","completed-mixed"])
async def test_corrupt_resolved_graph_is_not_an_executable_input(change):
    m,_,_,task,raw=graph(completed=change=="completed-mixed")
    if change=="extra":raw["extra"]="not allowed"
    if change=="generation":raw["run_generation"]=2
    if change=="case":raw["manifest"]["case_id"]=str(UUID(int=333))
    if change=="manifest":task=task.model_copy(update={"manifest_id":UUID(int=444)})
    if change=="query":raw["request"]["limits"]["max_findings"]-=1
    if change=="source":raw["request"]["evidence_input"]["sources"][0]["payload"]["discovered_url_count"]+=1
    if change=="request-null":raw["request"]=None
    if change=="completed-mixed":raw["request"]={}
    with pytest.raises(TransientJobError):await resolve(raw,task,m.job_id)


@pytest.mark.anyio
@pytest.mark.parametrize("mode",["acquisition","first_service","after_acquisition"])
async def test_prepare_keeps_only_recipe_and_sends_exact_source_and_full_generation_canonicals(mode):
    m,r,parent,_,_=graph(mode)
    calls=[]
    def respond(sent):
        calls.append(sent)
        return httpx.Response(200,json=[dict(manifest_id=str(m.id),input_checksum=m.input_checksum,generation_input_checksum=m.generation_input_checksum)])
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        saved=await SupabaseCapabilityInputRepository(url="https://storage.example",service_role_key="test-key",http_client=client).prepare(
            manifest=m,request=r,previous_report=parent,run_generation=1)
    assert saved==m and len(calls)==1
    body=json.loads(calls[0].content)
    assert verified_request_digest(json.loads(body["p_generation_canonical"]))==m.generation_input_checksum
    pub=r if mode=="acquisition" else r.public_input
    assert body["p_public_source_canonicals"]=={s.kind:canonical_json_bytes(s.payload).decode() for s in pub.evidence_input.sources}
    recipe=body["p_recipe"]["request"] if mode=="acquisition" else body["p_recipe"]["request"]["public_input"]
    assert all("payload" not in source for source in recipe["evidence_input"]["sources"])
    if mode!="acquisition":assert all("normalized_payload" not in s and "raw_payload" not in s for s in body["p_recipe"]["request"]["first_party_input"]["snapshots"])
