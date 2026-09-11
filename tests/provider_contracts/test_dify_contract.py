from uuid import UUID
import json

import httpx
import pytest

from app.jobs_v22.copy_provider import DifyControlledCopyProvider
from app.jobs_v22.errors import DeterministicJobError, TransientJobError
from v22_copy_helpers import copy_request


pytestmark = pytest.mark.contract
JOB_ID = UUID("55555555-5555-4555-8555-555555555555")


@pytest.mark.anyio
async def test_dify_fixture_matches_controlled_copy_request_boundary(provider_fixture) -> None:
    fixture = provider_fixture("dify/workflow_success.json")
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=fixture)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        output = await DifyControlledCopyProvider(
            api_key="fixture-key-value",
            api_url="https://dify.example.test/v1",
            model_version="fixture-model-v1",
            http_client=client,
        ).generate(job_id=JOB_ID, request=copy_request())

    body = json.loads(requests[0].content)
    assert requests[0].url.path == "/v1/workflows/run"
    assert set(body["inputs"]) == {"copy_request"}
    assert body["response_mode"] == "blocking"
    assert output == fixture["data"]["outputs"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("response", "error_type", "error_code"),
    [
        (httpx.Response(401, text="fixture-key-value"), DeterministicJobError, "V22_COPY_PROVIDER_REJECTED"),
        (httpx.Response(429, text="fixture-key-value"), TransientJobError, "V22_COPY_PROVIDER_UNAVAILABLE"),
        (httpx.Response(503, text="fixture-key-value"), TransientJobError, "V22_COPY_PROVIDER_UNAVAILABLE"),
    ],
)
async def test_dify_http_errors_are_stable_and_secret_safe(response, error_type, error_code) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: response)
    ) as client:
        provider = DifyControlledCopyProvider(
            api_key="fixture-key-value",
            api_url="https://dify.example.test/v1",
            model_version="fixture-model-v1",
            http_client=client,
        )
        with pytest.raises(error_type) as raised:
            await provider.generate(job_id=JOB_ID, request=copy_request())
    assert raised.value.error_code == error_code
    assert "fixture-key-value" not in str(raised.value)


@pytest.mark.anyio
async def test_dify_oversized_or_malformed_output_fails_closed() -> None:
    responses = [
        httpx.Response(200, text="not-json"),
        httpx.Response(200, json={"data": {"outputs": {"copy": "x" * 1_000_001}}}),
    ]
    for response in responses:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request, value=response: value)
        ) as client:
            provider = DifyControlledCopyProvider(
                api_key="fixture-key-value",
                api_url="https://dify.example.test/v1",
                model_version="fixture-model-v1",
                http_client=client,
            )
            assert await provider.generate(job_id=JOB_ID, request=copy_request()) == {}
