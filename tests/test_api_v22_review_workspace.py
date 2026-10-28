import json
import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr
from app.api.v2.review_evidence import router,workspace_service
from app.core.config import settings
from app.report_v22.review_workspace import ReviewWorkspace
from tests.test_v22_review_workspace import Store

@pytest.fixture
def anyio_backend():return 'asyncio'

@pytest.fixture
async def client(monkeypatch):
    monkeypatch.setattr(settings,'V22_INTERNAL_API_TOKEN',SecretStr('local-token'))
    monkeypatch.setattr(settings,'V22_CAPABILITY_EXECUTION_ENABLED',True)
    monkeypatch.setattr(settings,'V22_REVIEW_EVIDENCE_WORKSPACE_ENABLED',True)
    store=Store();app=FastAPI();app.include_router(router)
    app.dependency_overrides[workspace_service]=lambda:ReviewWorkspace(store)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://local') as client:
        yield client,store

HEADERS={'Authorization':'Bearer local-token','Content-Type':'application/json'}
@pytest.mark.anyio
async def test_workspace_internal_auth_and_flag(client,monkeypatch):
    c,s=client;body=s.command('preview').model_dump(mode='json')
    assert (await c.post('/api/v2/review-evidence',json=body)).status_code==401
    monkeypatch.setattr(settings,'V22_REVIEW_EVIDENCE_WORKSPACE_ENABLED',False)
    assert (await c.post('/api/v2/review-evidence',headers=HEADERS,json=body)).status_code==404
    assert s.resolves==s.writes==0

@pytest.mark.anyio
async def test_workspace_api_strict_bounded_private(client):
    c,s=client;body=s.command('preview').model_dump_json()
    response=await c.post('/api/v2/review-evidence',headers=HEADERS,content=body)
    assert response.status_code==200 and response.headers['cache-control']=='private, no-store'
    assert response.json()['state']=='preview'
    for raw,status in [(body[:-1]+',"operation":"preview"}',400),('x'*50000,413),('{',400),
                       (json.dumps(json.loads(body)|{'user_supplied_bundle':{}}),400)]:
        response=await c.post('/api/v2/review-evidence',headers=HEADERS,content=raw)
        assert response.status_code==status and set(response.json())=={'code'}
    assert s.writes==0
