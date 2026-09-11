import json

import httpx
import pytest

from app.collectors.site_inventory_firecrawl import FirecrawlMapAdapter


pytestmark = pytest.mark.contract


@pytest.mark.anyio
async def test_firecrawl_map_fixture_matches_active_adapter_contract(provider_fixture) -> None:
    fixture = provider_fixture("firecrawl/map_success.json")
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=fixture)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await FirecrawlMapAdapter(
            api_key="fixture-key-value",
            api_url="https://firecrawl.example.test/v1",
            enabled=True,
            timeout_seconds=2,
            http_client=client,
        ).map("https://fixture.example/", limit=10)

    assert result.urls == (
        "https://fixture.example/",
        "https://fixture.example/services",
    )
    assert result.limitation is None
    assert requests[0].url.path == "/v1/map"
    assert json.loads(requests[0].content) == {"url": "https://fixture.example/", "limit": 10}


@pytest.mark.anyio
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json={"success": True, "links": [], "unknown": True}),
        httpx.Response(200, text="not-json"),
        httpx.Response(429, json={"message": "private provider response"}),
        httpx.Response(503, json={}),
        httpx.Response(200, headers={"content-length": "1000001"}, content=b"{}"),
    ],
)
async def test_firecrawl_drift_errors_and_limits_fail_closed(response) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: response)
    ) as client:
        result = await FirecrawlMapAdapter(
            api_key="fixture-key-value",
            api_url="https://firecrawl.example.test/v1",
            enabled=True,
            timeout_seconds=2,
            http_client=client,
        ).map("https://fixture.example/", limit=10)
    assert result.urls == ()
    assert result.limitation == "firecrawl_unavailable"
