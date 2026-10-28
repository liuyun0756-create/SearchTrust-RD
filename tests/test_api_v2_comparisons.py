import json
from uuid import uuid4
import httpx
import pytest
from app.api.v2.comparisons import comparison_service
from app.core.config import settings
from app.main import create_app
from app.report_v22.comparison_entry import ComparisonContext
from app.jobs_v22.errors import DeterministicJobError, TransientJobError

pytestmark = pytest.mark.anyio

@pytest.fixture
def anyio_backend(): return "asyncio"

class Service:
    def __init__(self): self.calls = []; self.error = None
    async def run(self, command):
        self.calls.append(command)
        if self.error: raise self.error
        return ComparisonContext(schema_version="v22_comparison_context_v1", case_id=command.case_id,
            report_id=command.report_id, delivery_revision_id=command.delivery_revision_id, implementation=None, sources=[], history=[])

def setup(monkeypatch):
    monkeypatch.setattr(settings,"V22_COMPARISONS_ENABLED",True)
    monkeypatch.setattr(settings,"V22_CAPABILITY_EXECUTION_ENABLED",True)
    monkeypatch.setattr(settings,"V22_INTERNAL_API_TOKEN","local-test")
    service=Service(); app=create_app(); app.dependency_overrides[comparison_service]=lambda:service
    body=dict(operation="context",user_id=str(uuid4()),case_id=str(uuid4()),report_id=str(uuid4()),
        delivery_revision_id=str(uuid4()),comparison_id=None,selection=None)
    return app,service,body

async def test_comparison_context_without_queue_or_provider(monkeypatch):
    app,service,body=setup(monkeypatch)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as client:
        res=await client.post("/api/v2/comparisons",json=body,headers={"Authorization":"Bearer local-test"})
    assert res.status_code==200 and "no-store" in res.headers["cache-control"]
    assert res.json()["sources"]==[] and len(service.calls)==1
    assert set(res.json())=={"schema_version","case_id","report_id","delivery_revision_id","implementation","sources","history"}

@pytest.mark.parametrize("kind,status",[("auth",401),("flag",404),("execution",404),("extra",400),("shape",400),("large",413),("type",415)])
async def test_comparison_guards(monkeypatch,kind,status):
    app,service,body=setup(monkeypatch); headers={"Authorization":"Bearer local-test","Content-Type":"application/json"}
    if kind=="auth": headers.pop("Authorization")
    if kind=="flag": monkeypatch.setattr(settings,"V22_COMPARISONS_ENABLED",False)
    if kind=="execution": monkeypatch.setattr(settings,"V22_CAPABILITY_EXECUTION_ENABLED",False)
    if kind=="extra": body["snapshot"]={}
    if kind=="shape": body["operation"]="freeze"
    if kind=="type": headers["Content-Type"]="text/plain"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as client:
        res=await client.post("/api/v2/comparisons",content="x"*20001 if kind=="large" else json.dumps(body),headers=headers)
    assert res.status_code==status and not service.calls

@pytest.mark.parametrize("code,status",[("V22_COMPARISON_NOT_FOUND",404),("V22_COMPARISON_CONFLICT",409),("V22_COMPARISON_REJECTED",409),("OTHER",503),("TRANSIENT",503)])
async def test_comparison_safe_errors(monkeypatch,code,status):
    app,service,body=setup(monkeypatch)
    service.error=(TransientJobError if code=="TRANSIENT" else DeterministicJobError)(code,"private source content")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as client:
        res=await client.post("/api/v2/comparisons",json=body,headers={"Authorization":"Bearer local-test"})
    assert res.status_code==status and "private source" not in res.text
