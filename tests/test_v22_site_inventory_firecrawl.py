import httpx
import pytest

from app.collectors.site_inventory_firecrawl import FirecrawlMapAdapter


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_firecrawl_returns_bounded_urls_from_supported_shapes() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "success": True,
                "links": [
                    "https://example.com/a",
                    {"url": "https://example.com/b", "title": "B"},
                    {"title": "missing"},
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = FirecrawlMapAdapter(
            api_key="secret",
            api_url="https://api.firecrawl.dev/v1",
            enabled=True,
            timeout_seconds=5,
            http_client=client,
        )
        result = await adapter.map("https://example.com/", limit=2)

    assert result.urls == ("https://example.com/a", "https://example.com/b")
    assert result.limitation is None
    assert len(requests) == 1
    assert requests[0].url == "https://api.firecrawl.dev/v1/map"
    assert requests[0].headers["authorization"] == "Bearer secret"


@pytest.mark.anyio
async def test_firecrawl_skips_without_configuration() -> None:
    adapter = FirecrawlMapAdapter(
        api_key="",
        api_url="https://api.firecrawl.dev/v1",
        enabled=True,
        timeout_seconds=5,
    )

    result = await adapter.map("https://example.com/", limit=20)

    assert result.urls == ()
    assert result.limitation == "firecrawl_unavailable"


@pytest.mark.anyio
async def test_firecrawl_returns_safe_limitation_on_bad_response() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(429, text="secret upstream error"))
    ) as client:
        adapter = FirecrawlMapAdapter(
            api_key="secret",
            api_url="https://api.firecrawl.dev/v1",
            enabled=True,
            timeout_seconds=5,
            http_client=client,
        )
        result = await adapter.map("https://example.com/", limit=20)

    assert result.urls == ()
    assert result.limitation == "firecrawl_unavailable"


@pytest.mark.anyio
async def test_firecrawl_rejects_oversized_provider_response() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, content=b"x" * 101)
        )
    ) as client:
        adapter = FirecrawlMapAdapter(
            api_key="secret",
            api_url="https://api.firecrawl.dev/v1",
            enabled=True,
            timeout_seconds=5,
            http_client=client,
            max_response_bytes=100,
        )
        result = await adapter.map("https://example.com/", limit=20)

    assert result.urls == ()
    assert result.limitation == "firecrawl_unavailable"
