"""Transport budgets only: no database freeze, provider, or network calls."""
from contextlib import asynccontextmanager

import httpx
import pytest

from app.jobs_v22.capability_input_repository import SupabaseCapabilityInputRepository
from app.jobs_v22.errors import DeterministicJobError
from app.jobs_v22.verified_input_resolver import VerifiedRpcClient
from app.jobs_v22 import verified_input_resolver as transport_module


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_capability_input_rpcs_use_180_seconds_including_total_and_httpx_overrides(monkeypatch):
    total_budgets, requests = [], []

    @asynccontextmanager
    async def total_timeout(seconds):
        total_budgets.append(seconds)
        yield

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(transport_module.asyncio, "timeout", total_timeout)
    # Reproduce the Worker's existing 60s client default: per-request 180 wins.
    async with httpx.AsyncClient(timeout=60, transport=httpx.MockTransport(respond)) as client:
        repository = SupabaseCapabilityInputRepository(
            url="https://storage.example", service_role_key="test-only", http_client=client)
        assert repository.rpc.total_timeout_seconds == 180
        for name in ("prepare_v22_capability_analysis_input", "freeze_v22_acquisition_preparation",
                     "freeze_v22_first_service_preparation", "resolve_v22_capability_analysis_input"):
            assert await repository.rpc.post(name, {}) == {"ok": True}
    assert total_budgets == [180] * 4
    assert len(requests) == 4
    assert all(request.extensions["timeout"] == dict(connect=180, read=180, write=180, pool=180)
               for request in requests)


@pytest.mark.parametrize("prefix", ["V22_CAPABILITY_INPUT", "V22_VERIFIED_INPUT", "V22_CAPABILITY_PERSISTENCE"])
def test_shared_rpc_defaults_remain_20_seconds(prefix):
    rpc = VerifiedRpcClient(url="https://storage.example", service_role_key="test-only",
        http_client=None, max_response_bytes=100, prefix=prefix)
    assert rpc.total_timeout_seconds == 20


@pytest.mark.parametrize("prefix,seconds", [
    ("V22_CAPABILITY_INPUT", 0), ("V22_CAPABILITY_INPUT", -1), ("V22_CAPABILITY_INPUT", 180.001),
    ("V22_VERIFIED_INPUT", 61), ("V22_CAPABILITY_PERSISTENCE", 180), ("OTHER", 180),
])
def test_longer_budget_is_bounded_and_not_granted_to_other_prefixes(prefix, seconds):
    with pytest.raises(DeterministicJobError) as raised:
        VerifiedRpcClient(url="https://storage.example", service_role_key="test-only",
            http_client=None, max_response_bytes=100, prefix=prefix, total_timeout_seconds=seconds)
    assert raised.value.error_code == prefix + "_NOT_CONFIGURED"


@pytest.mark.parametrize("prefix", ["V22_VERIFIED_INPUT", "V22_CAPABILITY_PERSISTENCE", "OTHER"])
def test_other_clients_still_accept_the_original_60_second_maximum(prefix):
    rpc = VerifiedRpcClient(url="https://storage.example", service_role_key="test-only",
        http_client=None, max_response_bytes=100, prefix=prefix, total_timeout_seconds=60)
    assert rpc.total_timeout_seconds == 60
