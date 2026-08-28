import asyncio
from typing import Any

import httpx
import pytest

from app.collectors.site_inventory_fetcher import (
    AsyncRateLimiter,
    BoundedSiteFetcher,
    SiteFetchError,
)
from app.collectors.site_inventory_urls import SiteScope
from app.preflight_v22.urls import SafeUrl, UrlSafetyError


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def public_resolver(hostname: str) -> list[str]:
    if hostname == "private.example":
        return ["127.0.0.1"]
    return ["93.184.216.34"]


def build_fetcher(
    handler: Any,
    *,
    max_redirects: int = 3,
    concurrency: int = 10,
) -> tuple[BoundedSiteFetcher, list[SafeUrl]]:
    targets: list[SafeUrl] = []

    def client_factory(target: SafeUrl) -> httpx.AsyncClient:
        targets.append(target)
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    return (
        BoundedSiteFetcher(
            connect_timeout=1,
            read_timeout=1,
            total_timeout=3,
            max_redirects=max_redirects,
            concurrency=concurrency,
            requests_per_second=20,
            resolver=public_resolver,
            client_factory=client_factory,
        ),
        targets,
    )


@pytest.mark.anyio
async def test_fetches_bounded_html_with_pinned_target() -> None:
    fetcher, targets = build_fetcher(
        lambda request: httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            text="<h1>Example</h1>",
            request=request,
        )
    )

    result = await fetcher.fetch(
        "https://example.com/about",
        scope=SiteScope.from_root("https://example.com/"),
        max_bytes=256_000,
        accepted_media_types={"text/html", "application/xhtml+xml"},
    )

    assert result.final_url == "https://example.com/about"
    assert result.status_code == 200
    assert result.body == b"<h1>Example</h1>"
    assert targets[0].addresses == ("93.184.216.34",)
    assert result.decode() == "<h1>Example</h1>"


@pytest.mark.anyio
async def test_rejects_cross_host_redirect_before_second_request() -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://other.example/admin"})

    fetcher, _ = build_fetcher(handler)

    with pytest.raises(SiteFetchError, match="scope") as exc_info:
        await fetcher.fetch(
            "https://example.com/",
            scope=SiteScope.from_root("https://example.com/"),
            max_bytes=100,
        )

    assert exc_info.value.code == "unsafe_target"
    assert requests == ["https://example.com/"]


@pytest.mark.anyio
async def test_rejects_private_redirect_before_second_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://127.0.0.1/admin"})

    fetcher, _ = build_fetcher(handler)

    with pytest.raises(UrlSafetyError):
        await fetcher.fetch(
            "https://example.com/",
            scope=SiteScope.from_root("https://example.com/"),
            max_bytes=100,
        )


@pytest.mark.anyio
async def test_enforces_media_type_and_response_limit() -> None:
    fetcher, _ = build_fetcher(
        lambda request: httpx.Response(
            200,
            headers={"content-type": "application/pdf"},
            content=b"pdf",
            request=request,
        )
    )
    with pytest.raises(SiteFetchError) as unsupported:
        await fetcher.fetch(
            "https://example.com/file",
            scope=SiteScope.from_root("https://example.com/"),
            max_bytes=10,
            accepted_media_types={"text/html"},
        )
    assert unsupported.value.code == "content_unsupported"

    large_fetcher, _ = build_fetcher(
        lambda request: httpx.Response(
            200,
            headers={"content-type": "text/html"},
            content=b"x" * 11,
            request=request,
        )
    )
    with pytest.raises(SiteFetchError) as too_large:
        await large_fetcher.fetch(
            "https://example.com/",
            scope=SiteScope.from_root("https://example.com/"),
            max_bytes=10,
        )
    assert too_large.value.code == "response_too_large"


@pytest.mark.anyio
async def test_rate_limiter_spaces_request_starts() -> None:
    now = [0.0]
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    limiter = AsyncRateLimiter(
        requests_per_second=2,
        clock=lambda: now[0],
        sleep=fake_sleep,
    )

    await limiter.wait()
    await limiter.wait()
    await limiter.wait()

    assert sleeps == [0.5, 0.5]

    limiter.apply_minimum_delay(2)
    await limiter.wait()
    assert sleeps[-1] == 2


@pytest.mark.anyio
async def test_fetcher_enforces_concurrency_limit() -> None:
    active = 0
    maximum = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.01)
        active -= 1
        return httpx.Response(200, headers={"content-type": "text/html"}, text="ok")

    fetcher, _ = build_fetcher(handler, concurrency=1)
    scope = SiteScope.from_root("https://example.com/")

    await asyncio.gather(
        fetcher.fetch("https://example.com/a", scope=scope, max_bytes=100),
        fetcher.fetch("https://example.com/b", scope=scope, max_bytes=100),
    )

    assert maximum == 1
