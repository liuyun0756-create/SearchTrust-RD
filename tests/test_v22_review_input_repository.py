"""Bounded transport, unknown write results, and trusted input handoff."""
from copy import copy
import json
from uuid import uuid4

import httpx
import pytest

from app.jobs_v22.digest import canonical_json_bytes
from app.jobs_v22.errors import DeterministicJobError, TransientJobError
from app.jobs_v22.review_input_repository import (
    FrozenReviewGeneration, ReviewInputRepository, require_frozen_review_input, MAX_RECEIPT,
)
from app.report_v22.review_generation_models import recipe_for
from tests.test_v22_review_generation import generation


@pytest.fixture
def anyio_backend(): return "asyncio"


def receipt(value=None):
    value=value or generation()
    return dict(schema_version="v22_frozen_review_generation_v1",job_id=str(value.job_id),run_generation=1,
        frozen_at="2026-12-18T08:01:00Z",recipe=recipe_for(value).model_dump(mode="json"),
        input_text=canonical_json_bytes(value.model_dump(mode="json")).decode())


def identity(value):
    return dict(user_id=value.user_id,case_id=value.case_id,job_id=value.job_id,run_generation=1)


@pytest.mark.anyio
async def test_freeze_and_resolve_exact_binding_detach_and_process_seal():
    value=generation(); calls=[]
    def handler(req):
        calls.append(req)
        return httpx.Response(200,json=receipt(value))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        repo=ReviewInputRepository(url="https://local.invalid",service_role_key="local",http_client=client)
        frozen=await repo.freeze(value=value,run_generation=1)
        resolved=await repo.resolve(**identity(value),expected_checksum=value.checksum)
        assert require_frozen_review_input(resolved,run_generation=1)==value
        assert frozen.value is not value
        for fake in (copy(resolved),FrozenReviewGeneration(resolved.value,resolved.recipe,resolved.frozen_at),None):
            with pytest.raises(DeterministicJobError,match="UNTRUSTED_INPUT"): require_frozen_review_input(fake,run_generation=1)
        for run in (True,0,2):
            with pytest.raises(DeterministicJobError): require_frozen_review_input(resolved,run_generation=run)
        resolved.value.decision.decisions[0].reason="changed after resolve"
        with pytest.raises(DeterministicJobError): require_frozen_review_input(resolved,run_generation=1)
    assert len(calls)==2
    assert json.loads(calls[0].content)["p_input_text"]==canonical_json_bytes(value.model_dump(mode="json")).decode()
    assert "p_input_text" not in json.loads(calls[1].content)


@pytest.mark.anyio
@pytest.mark.parametrize("change",["owner","case","job","run","recipe","body","extra","json","duplicate","nonfinite","oversize","checksum","time"])
async def test_successful_but_unverifiable_write_is_unknown_not_refundable(change):
    value=generation(); raw=receipt(value)
    if change in {"owner","case"}:raw["recipe"]["user_id" if change=="owner" else "case_id"]=str(uuid4())
    elif change=="job":raw["job_id"]=str(uuid4())
    elif change=="run":raw["run_generation"]=True
    elif change=="recipe":raw["recipe"]["decision_id"]=str(uuid4())
    elif change=="body":raw["input_text"]="{}"
    elif change=="extra":raw["extra"]=True
    elif change=="time":raw["frozen_at"]="2026-12-01T00:00:00Z"
    elif change=="checksum":raw["recipe"]["input_checksum"]="sha256:"+"0"*64
    body=canonical_json_bytes(raw)
    if change=="json":body=b"{"
    elif change=="duplicate":body=body[:-1]+b',"job_id":"duplicate"}'
    elif change=="nonfinite":body=body.replace(b'"run_generation":1',b'"run_generation":NaN')
    elif change=="oversize":body=b" "*(MAX_RECEIPT+1)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _:httpx.Response(200,content=body))) as client:
        repo=ReviewInputRepository(url="https://local.invalid",service_role_key="local",http_client=client)
        with pytest.raises(TransientJobError,match="STORAGE_UNAVAILABLE"):
            await repo.freeze(value=value,run_generation=1)


@pytest.mark.anyio
@pytest.mark.parametrize("status,message,code,kind",[
    (400,"REVIEW_GENERATION_CONFLICT","CONFLICT",DeterministicJobError),
    (400,"REVIEW_GENERATION_INACTIVE","INACTIVE",DeterministicJobError),
    (400,"REVIEW_GENERATION_NOT_RESERVED","NOT_RESERVED",DeterministicJobError),
    (400,"secret database error","UNAVAILABLE",DeterministicJobError),
    (429,"secret database error","STORAGE_UNAVAILABLE",TransientJobError),
    (503,"secret database error","STORAGE_UNAVAILABLE",TransientJobError),
    (302,"redirect","UNAVAILABLE",DeterministicJobError)])
async def test_safe_errors_and_no_redirect(status,message,code,kind):
    calls=[]
    def handler(req):
        calls.append(req)
        return httpx.Response(status,json=dict(message=message),headers={"location":"https://elsewhere.invalid"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler),follow_redirects=True) as client:
        repo=ReviewInputRepository(url="https://local.invalid",service_role_key="local",http_client=client)
        with pytest.raises(kind,match=code) as caught:await repo.resolve(**identity(generation()))
        assert "secret" not in str(caught.value)+caught.value.user_message
    assert len(calls)==1


@pytest.mark.anyio
async def test_lost_ack_is_retryable_and_bad_requests_never_reach_transport():
    calls=[]
    def handler(req):
        calls.append(req);raise httpx.ReadTimeout("private",request=req)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        repo=ReviewInputRepository(url="https://local.invalid",service_role_key="local",http_client=client)
        value=generation()
        for run in (True,0,-1,"1",2147483647):
            with pytest.raises(DeterministicJobError):await repo.freeze(value=value,run_generation=run)
        with pytest.raises(DeterministicJobError):await repo.resolve(**(identity(value)|dict(job_id="invalid")))
        assert calls==[]
        with pytest.raises(TransientJobError):await repo.freeze(value=value,run_generation=1)
        assert len(calls)==1
