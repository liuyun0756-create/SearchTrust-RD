from pathlib import Path
from typing import Any

import httpcore
import httpx
import pytest

from app.preflight_v22.fetcher import (
    BoundedHomepageFetcher,
    HomepageFetchError,
    _PinnedNetworkBackend,
)
from app.preflight_v22.urls import SafeUrl, UrlSafetyError


FIXTURE = Path(__file__).parent / "fixtures" / "v22_preflight" / "homepage_complete.html"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def public_resolver(_: str) -> list[str]:
    return ["93.184.216.34"]


def fetcher_for(
    handler: Any,
    *,
    max_redirects: int = 3,
    max_response_bytes: int = 2_000_000,
) -> tuple[BoundedHomepageFetcher, list[SafeUrl]]:
    targets: list[SafeUrl] = []

    def client_factory(target: SafeUrl) -> httpx.AsyncClient:
        targets.append(target)
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    return (
        BoundedHomepageFetcher(
            connect_timeout=1,
            read_timeout=1,
            total_timeout=2,
            max_redirects=max_redirects,
            max_response_bytes=max_response_bytes,
            resolver=public_resolver,
            client_factory=client_factory,
        ),
        targets,
    )


@pytest.mark.anyio
async def test_fetcher_returns_bounded_html_snapshot() -> None:
    html = FIXTURE.read_text()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://example.com/"
        return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"}, text=html)

    fetcher, targets = fetcher_for(handler)
    snapshot = await fetcher.fetch("https://example.com/path?secret=1")

    assert snapshot.normalized_site_url == "https://example.com/"
    assert snapshot.source_url == "https://example.com/"
    assert "Acme Plumbing" in snapshot.html
    assert targets[0].addresses == ("93.184.216.34",)


@pytest.mark.anyio
async def test_fetcher_revalidates_each_redirect_before_request() -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://127.0.0.1/admin"})

    fetcher, _ = fetcher_for(handler)

    with pytest.raises(UrlSafetyError) as exc_info:
        await fetcher.fetch("https://example.com")

    assert exc_info.value.code == "URL_ADDRESS_FORBIDDEN"
    assert requests == ["https://example.com/"]


@pytest.mark.anyio
async def test_fetcher_preserves_safe_redirect_path_but_normalizes_site_identity() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/":
            return httpx.Response(302, headers={"location": "/home?from=redirect"})
        return httpx.Response(200, headers={"content-type": "text/html"}, text="<html>ok</html>")

    fetcher, targets = fetcher_for(handler)
    snapshot = await fetcher.fetch("https://example.com")

    assert targets[1].request_url == "https://example.com/home?from=redirect"
    assert snapshot.source_url == "https://example.com/home?from=redirect"
    assert snapshot.normalized_site_url == "https://example.com/"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("response", "expected_code"),
    [
        (httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"pdf"), "SITE_CONTENT_UNSUPPORTED"),
        (httpx.Response(503, headers={"content-type": "text/html"}, text="down"), "SITE_HTTP_ERROR"),
    ],
)
async def test_fetcher_classifies_unusable_responses(
    response: httpx.Response,
    expected_code: str,
) -> None:
    fetcher, _ = fetcher_for(lambda _: response)

    with pytest.raises(HomepageFetchError) as exc_info:
        await fetcher.fetch("https://example.com")

    assert exc_info.value.code == expected_code


@pytest.mark.anyio
async def test_fetcher_stops_when_response_exceeds_size_limit() -> None:
    response = httpx.Response(
        200,
        headers={"content-type": "text/html"},
        content=b"x" * 11,
    )
    fetcher, _ = fetcher_for(lambda _: response, max_response_bytes=10)

    with pytest.raises(HomepageFetchError) as exc_info:
        await fetcher.fetch("https://example.com")

    assert exc_info.value.code == "SITE_RESPONSE_TOO_LARGE"


@pytest.mark.anyio
async def test_fetcher_classifies_transport_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    fetcher, _ = fetcher_for(handler)

    with pytest.raises(HomepageFetchError) as exc_info:
        await fetcher.fetch("https://example.com")

    assert exc_info.value.code == "SITE_TIMEOUT"


@pytest.mark.anyio
async def test_pinned_backend_connects_to_validated_ip_not_hostname() -> None:
    calls: list[tuple[str, int]] = []
    stream = object()

    class Delegate(httpcore.AsyncNetworkBackend):
        async def connect_tcp(self, host: str, port: int, **_: Any) -> Any:
            calls.append((host, port))
            return stream

        async def connect_unix_socket(self, path: str, **_: Any) -> Any:
            raise AssertionError(path)

        async def sleep(self, seconds: float) -> None:
            return None

    target = SafeUrl(
        normalized_url="https://example.com/",
        request_url="https://example.com/",
        scheme="https",
        hostname="example.com",
        port=443,
        addresses=("93.184.216.34",),
    )
    backend = _PinnedNetworkBackend(target, delegate=Delegate())

    assert await backend.connect_tcp("example.com", 443) is stream
    assert calls == [("93.184.216.34", 443)]
