from __future__ import annotations

from uuid import UUID

import httpx
import pytest

from app.jobs_v22.copy_provider import DifyControlledCopyProvider
from app.jobs_v22.errors import DeterministicJobError, TransientJobError
from v22_copy_helpers import copy_request, valid_response


JOB_ID = UUID("55555555-5555-4555-8555-555555555555")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def provider(handler, *, api_key="secret"):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return DifyControlledCopyProvider(
        api_key=api_key,
        api_url="https://copy.example.test/v1/",
        model_version="dify-copy-fixture-v1",
        http_client=client,
    ), client


@pytest.mark.anyio
async def test_provider_sends_only_controlled_copy_contract_and_returns_outputs() -> None:
    request_value = copy_request()
    expected = valid_response(request_value)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://copy.example.test/v1/workflows/run"
        assert request.headers["Authorization"] == "Bearer secret"
        body = __import__("json").loads(request.content)
        assert set(body["inputs"]) == {"copy_request"}
        assert body["response_mode"] == "blocking"
        assert request_value == type(request_value).model_validate_json(
            body["inputs"]["copy_request"]
        )
        return httpx.Response(200, json={"data": {"outputs": expected}})

    value, client = provider(handler)
    try:
        assert await value.generate(job_id=JOB_ID, request=request_value) == expected
    finally:
        await client.aclose()


@pytest.mark.anyio
async def test_provider_classifies_configuration_and_remote_failures() -> None:
    missing, missing_client = provider(
        lambda request: httpx.Response(200),
        api_key="",
    )
    try:
        with pytest.raises(DeterministicJobError) as caught:
            await missing.generate(job_id=JOB_ID, request=copy_request())
        assert caught.value.error_code == "V22_COPY_PROVIDER_NOT_CONFIGURED"
    finally:
        await missing_client.aclose()

    unavailable, unavailable_client = provider(
        lambda request: httpx.Response(503),
    )
    try:
        with pytest.raises(TransientJobError) as caught:
            await unavailable.generate(job_id=JOB_ID, request=copy_request())
        assert caught.value.error_code == "V22_COPY_PROVIDER_UNAVAILABLE"
    finally:
        await unavailable_client.aclose()
