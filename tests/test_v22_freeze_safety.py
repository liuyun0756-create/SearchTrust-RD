"""Wire size and recovery checks never invoke a live database/provider."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
from arq import Retry

from app.core.config import Settings
from app.jobs_v22 import capability_acquisition_worker as worker
from app.jobs_v22.errors import DeterministicJobError
from app.jobs_v22.freeze_safety import DEFAULT_FREEZE_MAX_INPUT_BYTES, freeze_json_bytes
from test_v22_capability_acquisition_worker import Harness


@pytest.fixture
def anyio_backend(): return 'asyncio'


def test_freeze_default_is_decimal_40_mb_and_is_configurable():
    assert Settings(_env_file=None).V22_CAPABILITY_FREEZE_MAX_INPUT_BYTES == DEFAULT_FREEZE_MAX_INPUT_BYTES == 40_000_000
    assert Settings(_env_file=None,V22_CAPABILITY_FREEZE_MAX_INPUT_BYTES=1_000).V22_CAPABILITY_FREEZE_MAX_INPUT_BYTES == 1000
    with pytest.raises(ValueError):Settings(_env_file=None,V22_CAPABILITY_FREEZE_MAX_INPUT_BYTES=0)


@pytest.mark.anyio
@pytest.mark.parametrize('kind',['acquisition','first_service'])
async def test_exact_wire_limit_covers_wrapper_escaping_and_utf8_without_any_rpc(kind):
    body={'p_job_id':str(UUID(int=2)),'p_generation_canonical':'"\\中文"'*100}
    counters={'cost_ledger_revision':1};token=UUID(int=3)
    payload=dict(p_lease_token=str(token),p_cost_counters=counters,p_input=body)
    size=len(httpx.Request('POST','https://offline.invalid',json=payload).content)
    assert freeze_json_bytes(payload)==httpx.Request('POST','https://offline.invalid',json=payload).content
    assert size>len(freeze_json_bytes(body))
    rpc=SimpleNamespace(post=AsyncMock(return_value='same result'))
    before=AsyncMock(return_value=counters);snapshot=AsyncMock(return_value=counters)
    guard=SimpleNamespace(arm=AsyncMock())
    wrap=worker.LeasedPreparationRpc(rpc,token=token,kind=kind,before_freeze=before,
        cost_snapshot=snapshot,max_input_bytes=size-1,recovery=guard)
    with pytest.raises(DeterministicJobError) as caught:await wrap.post('prepare_v22_capability_analysis_input',body)
    assert caught.value.error_code=='V22_CAPABILITY_FREEZE_TOO_LARGE' and not caught.value.retryable
    rpc.post.assert_not_awaited();before.assert_not_awaited();guard.arm.assert_not_awaited()
    wrap.max_input_bytes=size
    assert await wrap.post('prepare_v22_capability_analysis_input',body)=='same result'
    rpc.post.assert_awaited_once_with('freeze_v22_'+kind+'_preparation',payload)


@pytest.mark.anyio
async def test_oversized_real_preparer_terminates_once_without_freeze(monkeypatch):
    h=Harness(monkeypatch);h.ctx['capability_freeze_max_input_bytes']=100
    try:
        await h.run()
        assert h.failed['p_error_code']=='V22_CAPABILITY_FREEZE_TOO_LARGE'
        assert not any(n.startswith('freeze_') or n=='inspect_v22_capability_preparation' for n,_ in h.calls)
        h.submit.assert_not_awaited()
        await h.run()
        assert sum(n=='apply_analysis_job_event' for n,_ in h.calls)==1
    finally:await h.close()


@pytest.mark.anyio
@pytest.mark.parametrize('restart,committed',[(False,False),(True,False),(True,True)])
async def test_disconnect_checks_database_boot_and_commit_before_retry_or_refund(monkeypatch,restart,committed):
    h=Harness(monkeypatch);original=h.respond
    def respond(request):
        name=request.url.path.rsplit('/',1)[-1]
        if name=='freeze_v22_acquisition_preparation':
            h.calls.append((name,json.loads(request.content)))
            h.frozen=committed
            if restart:h.database_started_at='2026-02-01T00:00:00Z'
            raise httpx.ReadError('private transport data',request=request)
        return original(request)
    h.client._transport=httpx.MockTransport(respond)
    try:
        if restart and not committed:
            await h.run()
            assert h.failed['p_error_code']=='V22_CAPABILITY_FREEZE_DATABASE_RESTARTED'
        else:
            with pytest.raises(Retry):await h.run()
            assert h.failed is None
        first=next(i for i,(n,_) in enumerate(h.calls) if n=='freeze_v22_acquisition_preparation')
        assert any(n=='inspect_v22_capability_preparation' for n,_ in h.calls[first+1:])
        h.client._transport=httpx.MockTransport(original)
        await h.run()
        assert sum(n=='freeze_v22_acquisition_preparation' for n,_ in h.calls)==(2 if not restart else 1)
        if committed:h.submit.assert_awaited_once()
    finally:await h.close()


@pytest.mark.anyio
async def test_unavailable_post_disconnect_probe_keeps_intent_and_checks_before_collection(monkeypatch):
    h=Harness(monkeypatch);original=h.respond;disconnected=False
    def respond(request):
        nonlocal disconnected
        name=request.url.path.rsplit('/',1)[-1]
        if name=='freeze_v22_acquisition_preparation':
            disconnected=True;h.calls.append((name,json.loads(request.content)))
            raise httpx.ReadError('private',request=request)
        if disconnected and name=='inspect_v22_capability_preparation':return httpx.Response(503)
        return original(request)
    h.client._transport=httpx.MockTransport(respond)
    try:
        with pytest.raises(Retry):await h.run()
        assert h.failed is None and h.collector.collect_sources.await_count==1
        h.database_started_at='2026-02-01T00:00:00Z';h.client._transport=httpx.MockTransport(original)
        await h.run()
        assert h.failed['p_error_code']=='V22_CAPABILITY_FREEZE_DATABASE_RESTARTED'
        assert h.collector.collect_sources.await_count==1
        assert sum(n=='freeze_v22_acquisition_preparation' for n,_ in h.calls)==1
    finally:await h.close()


@pytest.mark.anyio
async def test_first_service_size_failure_uses_terminal_path_but_other_errors_keep_original_retry(monkeypatch):
    from test_v22_first_service_worker import Harness as ServiceHarness
    for oversized in [False,True]:
        h=ServiceHarness(monkeypatch);failed=[];original=h.respond
        def respond(request):
            if request.url.path.endswith('/apply_analysis_job_event'):
                failed.append(json.loads(request.content))
                return httpx.Response(200,json=[{'found':True,'applied':True,'terminal_effects_applied':True}])
            return original(request)
        h.client._transport=httpx.MockTransport(respond)
        if oversized:h.ctx['capability_freeze_max_input_bytes']=100
        else:h.collector.collect_confirmed_sources.side_effect=DeterministicJobError('SOURCE_INVALID','private')
        try:
            if oversized:
                await worker.prepare_v22_first_service(h.ctx,str(h.args['job_id']),1)
                assert len(failed)==1 and failed[0]['p_error_code']=='V22_CAPABILITY_FREEZE_TOO_LARGE'
                assert not any(n.startswith('freeze_') for n,_ in h.calls)
            else:
                with pytest.raises(Retry):await worker.prepare_v22_first_service(h.ctx,str(h.args['job_id']),1)
                assert not failed
        finally:await h.close()
