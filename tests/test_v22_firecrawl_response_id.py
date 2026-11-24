"""The v2 response actually observed in Worker includes a provider request ID."""
import json
import logging
from pathlib import Path

import httpx
import pytest

from app.collectors.site_inventory_firecrawl import FirecrawlMapAdapter


@pytest.fixture
def anyio_backend():
    return "asyncio"


def observed_shape():
    return json.loads((Path(__file__).parent /
        "fixtures/provider_contracts/firecrawl/map_v2_with_id_redacted.json").read_text())


async def map_response(payload):
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json=payload))) as client:
        return await FirecrawlMapAdapter(api_key="PRIVATE_KEY", api_url="https://provider.example/v2",
            enabled=True, timeout_seconds=5, http_client=client).map("https://customer.example/", limit=200)


@pytest.mark.anyio
async def test_worker_observed_v2_id_shape_returns_urls_and_logs_provider_request_id(caplog):
    payload = observed_shape()
    assert set(payload) == {"id", "links", "success"}
    assert all(set(link) == {"url", "title", "description"} for link in payload["links"])
    with caplog.at_level(logging.INFO, logger="app.collectors.site_inventory_firecrawl"):
        result = await map_response(payload)
    assert len(result.urls) == 18 and result.limitation is None
    assert f'provider_request_id={json.dumps(payload["id"])}' in caplog.text
    assert "failure_stage=none http_status=200 exception_type=None" in caplog.text
    for private in ("PRIVATE_KEY", "provider.example", "customer.example", "fixture.example", "Redacted page description"):
        assert private not in caplog.text


@pytest.mark.anyio
@pytest.mark.parametrize("invalid_id", [None, 1, True, [], {}, "", "x" * 129])
async def test_provider_request_id_must_be_a_nonempty_bounded_string(invalid_id, caplog):
    payload = observed_shape()
    payload["id"] = invalid_id
    with caplog.at_level(logging.INFO, logger="app.collectors.site_inventory_firecrawl"):
        result = await map_response(payload)
    assert result.urls == () and result.limitation == "firecrawl_unavailable"
    assert "failure_stage=schema http_status=200 exception_type=ValueError" in caplog.text
    assert "provider_request_id=null" in caplog.text


@pytest.mark.anyio
async def test_provider_request_id_length_boundary_and_log_escaping(caplog):
    payload = observed_shape()
    payload["id"] = "r" * 126 + "\nX"
    assert len(payload["id"]) == 128
    with caplog.at_level(logging.INFO, logger="app.collectors.site_inventory_firecrawl"):
        result = await map_response(payload)
    assert result.limitation is None and len(result.urls) == 18
    records = [r for r in caplog.records if r.name == "app.collectors.site_inventory_firecrawl"]
    assert len(records) == 1 and "\n" not in records[0].message
    assert f'provider_request_id={json.dumps(payload["id"])}' in records[0].message


@pytest.mark.anyio
async def test_other_unknown_fields_still_fail_closed_with_a_valid_request_id(caplog):
    payload = observed_shape()
    payload["unexpected"] = "PRIVATE_BODY"
    with caplog.at_level(logging.INFO, logger="app.collectors.site_inventory_firecrawl"):
        result = await map_response(payload)
    assert result.urls == () and result.limitation == "firecrawl_unavailable"
    assert "failure_stage=schema" in caplog.text
    assert "PRIVATE_BODY" not in caplog.text
