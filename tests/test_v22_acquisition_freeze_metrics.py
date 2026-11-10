"""Freeze diagnostics preserve the RPC and never print private inputs."""
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
import httpx

from app.jobs_v22 import capability_acquisition_worker as worker
from app.jobs_v22.errors import DeterministicJobError, TransientJobError


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
@pytest.mark.parametrize("error", [None, DeterministicJobError("SOURCE_INVALID", "private name"),
    TransientJobError("STORAGE_UNAVAILABLE", "private phone")])
async def test_freeze_logs_exact_utf8_sizes_duration_and_versions_without_payload(monkeypatch, caplog, error):
    body = dict(p_job_id="12345678-1234-4123-8123-123456789abc", p_manifest={"schema_version":"v22_analysis_input_v3"},
        p_public_source_canonicals={"site":"private name 电话", "serp":"private query", "competitor":"private email",
            "public_gbp":"private phone", "rendered":"private HTML", "private_source_key":"private content"})
    result={"manifest_id":"private identity"}
    rpc=SimpleNamespace(post=AsyncMock(return_value=result,side_effect=error))
    before=AsyncMock(return_value={"job_attempts":1})
    wrapper=worker.LeasedPreparationRpc(rpc,token=UUID(int=1),before_freeze=before)
    times=iter([10.0,10.25]);monkeypatch.setattr(worker,"monotonic",lambda:next(times))
    caplog.set_level(logging.INFO,logger=worker.__name__)
    if error:
        with pytest.raises(type(error)) as caught: await wrapper.post("prepare_v22_capability_analysis_input",body)
        assert caught.value is error
    else:
        assert await wrapper.post("prepare_v22_capability_analysis_input",body) is result
    rpc.post.assert_awaited_once_with("freeze_v22_acquisition_preparation",dict(
        p_lease_token=str(UUID(int=1)),p_cost_counters={"job_attempts":1},p_input=body))
    messages=[r.getMessage() for r in caplog.records if r.name==worker.__name__]
    assert len(messages)==2 and "started elapsed_ms=0" in messages[0] and "finished elapsed_ms=250" in messages[1]
    expected_bytes=len(httpx.Request("POST","https://offline.example.invalid",json=body).content)
    for message in messages:
        assert f"input_bytes={expected_bytes}" in message
        assert "manifest_schema_version=v22_analysis_input_v3" in message and "job_id_suffix=56789abc" in message
        for kind in ("site","serp","competitor","public_gbp","rendered"):
            assert f"{kind}_canonical_bytes={len(body['p_public_source_canonicals'][kind].encode('utf-8'))}" in message
        assert "private" not in message and "电话" not in message and body["p_job_id"] not in message


@pytest.mark.anyio
async def test_invalid_diagnostic_fields_do_not_change_the_rpc_or_expose_unknown_version(caplog):
    rpc=SimpleNamespace(post=AsyncMock(return_value="same result"))
    wrapper=worker.LeasedPreparationRpc(rpc,token=UUID(int=1),before_freeze=AsyncMock(return_value={}))
    body={"p_job_id":"private data","p_manifest":{"schema_version":"private version"}}
    caplog.set_level(logging.INFO,logger=worker.__name__)
    assert await wrapper.post("prepare_v22_capability_analysis_input",body)=="same result"
    assert rpc.post.call_args.args[1]["p_input"] is body
    assert "private" not in caplog.text and "input_bytes=-1" in caplog.text
