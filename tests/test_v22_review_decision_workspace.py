"""Frozen source projection and default-off HTTP integration, without provider I/O."""
from copy import deepcopy
import json
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr

from app.api.v2.review_decisions import router, decision_workspace
from app.core.config import settings
from app.jobs_v22.errors import DeterministicJobError, TransientJobError
from app.report_v22.review_decision_workspace import context, parse_command, DecisionWorkspace
from app.report_v22.review_decision_service import ReviewDecisionService
from app.report_v22.review_evidence_validation import source_references
from tests.test_v22_review_decisions import example, request, receipt


@pytest.fixture
def anyio_backend(): return "asyncio"


class Store:
    def __init__(self):
        self.saved,self.ids=example(); self.calls=[]; self.records={}
    async def read(self,**ids):
        return self.saved if all(str(getattr(self.saved,k))==str(v) for k,v in ids.items() if k!="review_id") and str(self.saved.id)==str(ids["review_id"]) else None
    async def rpc(self,name,args):
        self.calls.append(name)
        if name=="save_v22_review_decision":
            req=json.loads(args["p_request_text"])
            if req["id"] not in self.records:self.records[req["id"]]=receipt(self.saved,req)
            return self.records[req["id"]]
        if name=="get_v22_review_decision":
            if args["p_id"] not in self.records:raise DeterministicJobError("V22_REVIEW_DECISION_UNAVAILABLE","private")
            return self.records[args["p_id"]]
        return dict(review_id=str(self.saved.id),report_id=str(self.saved.bundle.report.id),items=[{k:r[k] for k in ("id","revision","previous_id","recorded_at","checksum")}|dict(decision_count=len(r["decisions"])) for r in self.records.values()],next_cursor=None)
    def command(self,operation,**kw):
        raw=dict(operation=operation,user_id=str(self.saved.user_id),case_id=str(self.saved.case_id),report_id=str(self.saved.bundle.report.id))
        if operation!="save":raw["review_id"]=str(self.saved.id)
        return raw|kw


def test_context_exact_frozen_approved_actions_and_refs():
    s,_=example();before=s.model_dump_json();c=context(s)
    assert s.model_dump_json()==before and c.evidence.state=="saved"
    assert c.approved[0].steps==s.bundle.delivery.actions[0].steps
    for selection in s.bundle.selections:
        assert sorted(r.id for r in c.references if r.action_id==selection.action_id)==source_references(s.bundle,selection)
    text=c.model_dump_json()
    for key in ('"bundle"','"implementation_entry"','"normalized_payload"','"user_id"','"connection_id"','"lease_id"'):
        assert key not in text
    assert c.evidence.actions[0].effect is None


@pytest.mark.parametrize("patch",[dict(operation="generate"),dict(review_id="bad"),dict(cursor=0),dict(cursor=True),dict(cursor=2147483647),dict(user_supplied_bundle={}),dict(decisions=[]),dict(operation="read"),dict(operation="save")])
def test_strict_commands(patch):
    with pytest.raises(DeterministicJobError):parse_command(json.dumps(Store().command("history")|patch).encode())


@pytest.mark.parametrize("raw",[b'{"operation":"context","operation":"context"}',b'{"cursor":NaN}',b'{"cursor":Infinity}',b'[]',b'null',b'{',b'x'*73729])
def test_strict_json(raw):
    with pytest.raises(DeterministicJobError):parse_command(raw)


@pytest.mark.anyio
async def test_service_routing_no_resolve_and_wrong_context():
    s=Store();service=DecisionWorkspace(ReviewDecisionService(s,s));run=lambda o,**kw:service.run(parse_command(json.dumps(s.command(o,**kw)).encode()))
    assert (await run("context")).approved
    req=request(s.saved);r=await run("save",request=req)
    assert (await run("read",decision_id=str(r.id)))==r
    assert len((await run("history")).items)==1
    assert (await run("save",request=req))==r
    for key in ("user_id","case_id","report_id","review_id"):
        with pytest.raises(DeterministicJobError,match="UNAVAILABLE"):await run("context",**{key:str(uuid4())})
    assert not any("resolve" in n or "generate" in n for n in s.calls)


HEADERS={"Authorization":"Bearer local-token","Content-Type":"application/json"}
@pytest.fixture
async def client(monkeypatch):
    for name in ("V22_CAPABILITY_EXECUTION_ENABLED","V22_REVIEW_EVIDENCE_WORKSPACE_ENABLED","V22_REVIEW_DECISIONS_WORKSPACE_ENABLED"):monkeypatch.setattr(settings,name,True)
    monkeypatch.setattr(settings,"V22_INTERNAL_API_TOKEN",SecretStr("local-token"))
    s=Store();app=FastAPI();app.include_router(router);app.dependency_overrides[decision_workspace]=lambda:DecisionWorkspace(ReviewDecisionService(s,s))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url="http://local") as c:yield c,s


@pytest.mark.anyio
@pytest.mark.parametrize("flag",["V22_CAPABILITY_EXECUTION_ENABLED","V22_REVIEW_EVIDENCE_WORKSPACE_ENABLED","V22_REVIEW_DECISIONS_WORKSPACE_ENABLED"])
async def test_auth_and_flags(client,monkeypatch,flag):
    c,s=client;assert (await c.post("/api/v2/review-decisions",json=s.command("context"))).status_code==401
    monkeypatch.setattr(settings,flag,False)
    assert (await c.post("/api/v2/review-decisions",headers=HEADERS,json=s.command("context"))).status_code==404
    assert not s.calls


@pytest.mark.anyio
async def test_http_context_and_roundtrip(client):
    c,s=client;url="/api/v2/review-decisions"
    r=await c.post(url,headers=HEADERS,json=s.command("context"));assert r.status_code==200 and r.headers["cache-control"]=="private, no-store"
    r=await c.post(url,headers=HEADERS,json=s.command("save",request=request(s.saved)));assert r.status_code==200
    assert (await c.post(url,headers=HEADERS,json=s.command("read",decision_id=r.json()["id"]))).json()==r.json()
    body=json.dumps(s.command("context"))
    for raw,status in [(body[:-1]+',"review_id":"'+str(s.saved.id)+'"}',400),('x'*73729,413),('{',400)]:
        r=await c.post(url,headers=HEADERS,content=raw);assert r.status_code==status and set(r.json())=={"code"}


@pytest.mark.anyio
@pytest.mark.parametrize("failure,status,code",[(DeterministicJobError("V22_REVIEW_DECISION_STALE","private"),409,"STALE"),(TransientJobError("V22_REVIEW_DECISION_STORAGE_UNAVAILABLE","private"),503,"UNAVAILABLE")])
async def test_safe_errors(client,failure,status,code):
    c,s=client
    async def fail(*a,**kw):raise failure
    s.rpc=fail
    r=await c.post("/api/v2/review-decisions",headers=HEADERS,json=s.command("history"))
    assert r.status_code==status and r.json()==dict(code=code) and "private" not in r.text
