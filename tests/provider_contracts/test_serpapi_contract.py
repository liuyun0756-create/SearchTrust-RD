import httpx
import pytest

from app.integrations.serpapi import (
    SerpApiHttpError,
    SerpApiInvalidResponse,
    SerpApiKeysUnavailable,
    SerpApiTransportError,
    execute_serpapi_get,
)


pytestmark = pytest.mark.contract


@pytest.mark.anyio
async def test_serpapi_success_fixture_matches_active_client_contract(provider_fixture) -> None:
    fixture = provider_fixture("serpapi/search_success.json")
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=fixture)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await execute_serpapi_get(
            client,
            {"engine": "google", "q": "fixture plumbing"},
            keys=["fixture-key-value"],
            base_url="https://serpapi.example.test/search.json",
        )

    assert result.payload["search_metadata"]["status"] == "Success"
    assert result.payload["organic_results"][0]["position"] == 1
    assert requests[0].url.params["api_key"] == "fixture-key-value"
    assert "fixture-key-value" not in repr(result)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("response", "error_type"),
    [
        (httpx.Response(200, text="not-json"), SerpApiInvalidResponse),
        (httpx.Response(401, json={"error": "invalid api key"}), SerpApiKeysUnavailable),
        (httpx.Response(500, json={}), SerpApiHttpError),
    ],
)
async def test_serpapi_error_mapping_is_stable_and_secret_safe(response, error_type) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: response)
    ) as client:
        with pytest.raises(error_type) as raised:
            await execute_serpapi_get(
                client,
                {"engine": "google", "q": "fixture"},
                keys=["fixture-key-value"],
                base_url="https://serpapi.example.test/search.json",
            )
    assert "fixture-key-value" not in str(raised.value)


@pytest.mark.anyio
async def test_serpapi_timeout_is_transport_error_and_does_not_retry_other_keys() -> None:
    calls = 0

    def fail(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectTimeout("provider body with fixture-key-value")

    async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as client:
        with pytest.raises(SerpApiTransportError) as raised:
            await execute_serpapi_get(
                client,
                {"engine": "google"},
                keys=["fixture-key-value", "fixture-key-two"],
                base_url="https://serpapi.example.test/search.json",
            )
    assert calls == 1
    assert "fixture-key-value" not in str(raised.value)
