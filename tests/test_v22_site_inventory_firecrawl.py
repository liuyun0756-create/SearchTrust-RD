import httpx
import pytest
import json
import logging
from pathlib import Path
from uuid import UUID

from app.collectors.site_inventory_firecrawl import FirecrawlMapAdapter, firecrawl_job_context


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
    assert requests[0].url == "https://api.firecrawl.dev/v2/map"
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


@pytest.mark.anyio
@pytest.mark.parametrize("version", [1, 2])
async def test_map_accepts_saved_v1_metadata_and_v2_link_objects(version, caplog):
    fixture = Path(__file__).parent / f"fixtures/provider_contracts/firecrawl/map_v{version}_metadata_redacted.json"
    payload = json.loads(fixture.read_text())
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=payload)
    with caplog.at_level(logging.INFO, logger="app.collectors.site_inventory_firecrawl"):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = FirecrawlMapAdapter(api_key="PRIVATE_KEY", api_url="https://provider.example/v1",
                enabled=True, timeout_seconds=5, http_client=client)
            with firecrawl_job_context(UUID("55555555-5555-4555-8555-123456789abc")):
                result = await adapter.map("https://PRIVATE_CUSTOMER.example/", limit=200)
    assert len(result.urls) == 18 and result.limitation is None
    assert requests[0].url.path == "/v2/map"
    assert "failure_stage=none http_status=200 exception_type=None" in caplog.text
    assert f"warnings_count={1 if version == 1 else 0}" in caplog.text
    assert "job_id_suffix=56789abc" in caplog.text
    assert "elapsed_ms=" in caplog.text
    for secret in ("PRIVATE_KEY", "PRIVATE_CUSTOMER", "fixture.example", "deprecated", "/v2/map"):
        assert secret not in caplog.text


@pytest.mark.anyio
@pytest.mark.parametrize("status", [401, 402, 429, 500, 502, 503, 504])
async def test_map_http_failures_log_status_and_type_without_content(status, caplog):
    with caplog.at_level(logging.INFO, logger="app.collectors.site_inventory_firecrawl"):
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _: httpx.Response(status, text="PRIVATE_RESPONSE"))) as client:
            result = await FirecrawlMapAdapter(api_key="PRIVATE_KEY", api_url="https://PRIVATE_PROVIDER/v2",
                enabled=True, timeout_seconds=5, http_client=client).map("https://PRIVATE_CUSTOMER/", limit=2)
    assert result.urls == () and result.limitation == "firecrawl_unavailable"
    assert f"failure_stage=http http_status={status} exception_type=HTTPStatusError" in caplog.text
    assert "PRIVATE_" not in caplog.text


@pytest.mark.anyio
@pytest.mark.parametrize("kind,expected", [("network", "network"), ("json", "json"),
    ("unknown", "schema"), ("links", "schema"), ("warnings", "schema"), ("replacement", "schema")])
async def test_map_failure_phase_is_specific_and_safe(kind, expected, caplog):
    def handler(request):
        if kind == "network":
            raise httpx.ConnectError("PRIVATE_NETWORK_DETAILS", request=request)
        if kind == "json":return httpx.Response(200, text="PRIVATE_INVALID_JSON")
        payload = {"success": True, "links": []}
        payload[kind] = "PRIVATE_INVALID_VALUE" if kind != "replacement" else 123
        return httpx.Response(200, json=payload)
    with caplog.at_level(logging.INFO, logger="app.collectors.site_inventory_firecrawl"):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await FirecrawlMapAdapter(api_key="PRIVATE_KEY", api_url="https://provider.example",
                enabled=True, timeout_seconds=5, http_client=client).map("https://PRIVATE_CUSTOMER/", limit=2)
    assert result.limitation == "firecrawl_unavailable"
    assert f"failure_stage={expected}" in caplog.text
    assert "PRIVATE_" not in caplog.text


@pytest.mark.anyio
async def test_map_disabled_is_observed_without_network_and_context_is_reset(caplog):
    def forbidden(_):raise AssertionError("NO_HTTP_ALLOWED")
    with caplog.at_level(logging.INFO, logger="app.collectors.site_inventory_firecrawl"):
        async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
            adapter = FirecrawlMapAdapter(api_key="unused", api_url="https://provider.example/v1",
                enabled=False, timeout_seconds=5, http_client=client)
            with firecrawl_job_context(UUID("55555555-5555-4555-8555-123456789abc")):
                await adapter.map("https://customer.example/", limit=2)
            await adapter.map("https://customer.example/", limit=2)
    lines = [r.message for r in caplog.records if r.name == "app.collectors.site_inventory_firecrawl"]
    assert len(lines) == 2 and all("failure_stage=disabled http_status=None" in line for line in lines)
    assert "job_id_suffix=56789abc" in lines[0] and "job_id_suffix=unknown" in lines[1]


@pytest.mark.anyio
async def test_checkpoint_wrapper_supplies_job_context_and_preserves_checkpoint_shape(caplog):
    import fakeredis.aioredis
    from app.jobs_v22.checkpoints import JobCheckpoints
    from app.jobs_v22.site_inventory_stage import _CheckpointedFirecrawl
    redis = fakeredis.aioredis.FakeRedis()
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"success": True, "links": []})
    try:
        with caplog.at_level(logging.INFO, logger="app.collectors.site_inventory_firecrawl"):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                adapter = FirecrawlMapAdapter(api_key="fixture", api_url="https://provider.example/v1",
                    enabled=True, timeout_seconds=5, http_client=client)
                wrapped = _CheckpointedFirecrawl(delegate=adapter,
                    checkpoints=JobCheckpoints(redis, prefix="map-context", ttl_seconds=3600),
                    job_id=UUID("55555555-5555-4555-8555-123456789abc"))
                assert await wrapped.map("https://customer.example/", limit=2) == await wrapped.map("https://customer.example/", limit=2)
        assert len(requests) == 1
        assert "job_id_suffix=56789abc" in caplog.text
        values = [json.loads(await redis.get(key)) for key in await redis.keys("*:checkpoint:*")]
        assert values == [{"schema_version": "site_inventory_firecrawl_v1", "urls": [], "limitation": None}]
    finally:
        await redis.aclose()
