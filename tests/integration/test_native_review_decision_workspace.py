"""HTTP-to-service-to-disposable database: free owner decision workflow."""
import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr
from app.api.v2.review_decisions import router, decision_workspace
from app.core.config import settings
from app.report_v22.review_decision_workspace import DecisionWorkspace
from tests.integration.test_native_review_decisions import decisions
from tests.integration.test_native_review_freeze import review_setup, prepared, native_database, inspect
from tests.test_v22_review_decisions import request

pytestmark=[pytest.mark.native_integration,pytest.mark.anyio,pytest.mark.timeout(70)]


async def test_native_workspace_context_save_read_history(decisions,monkeypatch):
    p=decisions;s=p["evidence"]
    for name in ("V22_CAPABILITY_EXECUTION_ENABLED","V22_REVIEW_EVIDENCE_WORKSPACE_ENABLED","V22_REVIEW_DECISIONS_WORKSPACE_ENABLED"):monkeypatch.setattr(settings,name,True)
    monkeypatch.setattr(settings,"V22_INTERNAL_API_TOKEN",SecretStr("local-token"))
    app=FastAPI();app.include_router(router);app.dependency_overrides[decision_workspace]=lambda:DecisionWorkspace(p["decision_service"])
    common={k:str(v) for k,v in p["owner"].items()};headers={"Authorization":"Bearer local-token"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url="http://local") as c:
        async def post(operation,**kw):return await c.post("/api/v2/review-decisions",headers=headers,json=common|dict(operation=operation)|kw)
        context=await post("context",review_id=str(s.id));assert context.status_code==200
        assert context.json()["approved"][0]["steps"]==s.bundle.delivery.actions[0].steps
        first=await post("save",request=request(s));assert first.status_code==200
        r=first.json();second=await post("save",request=request(s,[],previous_id=r["id"],previous_checksum=r["checksum"]));assert second.status_code==200 and second.json()["revision"]==2
        assert (await post("read",review_id=str(s.id),decision_id=r["id"])).json()==r
        assert len((await post("history",review_id=str(s.id))).json()["items"])==2
        assert (await post("save",request=request(s))).status_code==409
        assert (await post("context",review_id=str(s.id))).json()==context.json()
    assert await inspect(p["db"],p["scenario"])==p["baseline"]
