"""Bounded, DNS-pinned HTTP fetches for v2.2 site inventory collection."""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

import httpx

from app.collectors.site_inventory_urls import SiteScope
from app.preflight_v22.fetcher import _PinnedAsyncHTTPTransport
from app.preflight_v22.urls import Resolver, SafeUrl, resolve_public_url


ClientFactory = Callable[[SafeUrl], httpx.AsyncClient]
Clock = Callable[[], float]
Sleeper = Callable[[float], Awaitable[None]]
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_CHARSET_PATTERN = re.compile(r"charset\s*=\s*['\"]?([^;\s'\"]+)", re.IGNORECASE)


class SiteFetchError(OSError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.user_message = message


@dataclass(frozen=True)
class SiteFetchResponse:
    requested_url: str
    final_url: str
    status_code: int
    content_type: str
    body: bytes

    @property
    def response_bytes(self) -> int:
        return len(self.body)

    def decode(self) -> str:
        match = _CHARSET_PATTERN.search(self.content_type)
        encoding = match.group(1) if match else "utf-8"
        try:
            return self.body.decode(encoding, errors="replace")
        except LookupError:
            return self.body.decode("utf-8", errors="replace")


class AsyncRateLimiter:
    def __init__(
        self,
        *,
        requests_per_second: float,
        clock: Clock = time.monotonic,
        sleep: Sleeper = asyncio.sleep,
    ) -> None:
        if requests_per_second <= 0:
            raise ValueError("requests per second must be positive")
        self.interval = 1.0 / requests_per_second
        self.clock = clock
        self.sleep = sleep
        self._next_allowed = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = self.clock()
            delay = max(0.0, self._next_allowed - now)
            if delay:
                await self.sleep(delay)
                now = self.clock()
            self._next_allowed = max(now, self._next_allowed) + self.interval

    def apply_minimum_delay(self, seconds: float) -> None:
        if seconds > 0:
            self.interval = max(self.interval, seconds)
            self._next_allowed = max(self._next_allowed, self.clock() + self.interval)


class BoundedSiteFetcher:
    def __init__(
        self,
        *,
        connect_timeout: float,
        read_timeout: float,
        total_timeout: float,
        max_redirects: int,
        concurrency: int,
        requests_per_second: float,
        resolver: Resolver | None = None,
        client_factory: ClientFactory | None = None,
        rate_limiter: AsyncRateLimiter | None = None,
    ) -> None:
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.total_timeout = total_timeout
        self.max_redirects = max_redirects
        self.resolver = resolver
        self.client_factory = client_factory or self._default_client
        self.rate_limiter = rate_limiter or AsyncRateLimiter(
            requests_per_second=requests_per_second
        )
        self._semaphore = asyncio.Semaphore(concurrency)

    def apply_crawl_delay(self, seconds: float | None) -> None:
        if seconds is not None:
            self.rate_limiter.apply_minimum_delay(seconds)

    def _default_client(self, target: SafeUrl) -> httpx.AsyncClient:
        timeout = httpx.Timeout(
            connect=self.connect_timeout,
            read=self.read_timeout,
            write=self.read_timeout,
            pool=self.connect_timeout,
        )
        return httpx.AsyncClient(
            transport=_PinnedAsyncHTTPTransport(target),
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
        )

    async def fetch(
        self,
        url: str,
        *,
        scope: SiteScope,
        max_bytes: int,
        accepted_media_types: set[str] | None = None,
    ) -> SiteFetchResponse:
        async with self._semaphore:
            try:
                return await asyncio.wait_for(
                    self._fetch_redirects(
                        url,
                        scope=scope,
                        max_bytes=max_bytes,
                        accepted_media_types=accepted_media_types,
                    ),
                    timeout=self.total_timeout,
                )
            except TimeoutError as exc:
                raise SiteFetchError("page_timeout", "The page request timed out.") from exc
            except httpx.TimeoutException as exc:
                raise SiteFetchError("page_timeout", "The page request timed out.") from exc
            except httpx.HTTPError as exc:
                raise SiteFetchError("http_error", "The page could not be reached.") from exc

    async def _fetch_redirects(
        self,
        initial_url: str,
        *,
        scope: SiteScope,
        max_bytes: int,
        accepted_media_types: set[str] | None,
    ) -> SiteFetchResponse:
        current_url = initial_url
        for redirect_count in range(self.max_redirects + 1):
            target = await resolve_public_url(current_url, resolver=self.resolver)
            if not scope.contains_host(target.hostname):
                raise SiteFetchError("unsafe_target", "The request left the allowed site scope.")
            await self.rate_limiter.wait()
            async with self.client_factory(target) as client:
                async with client.stream(
                    "GET",
                    target.request_url,
                    headers={
                        "User-Agent": (
                            "SearchTrust-SiteInventory/2.2 "
                            "(+https://trysearchtrust.com)"
                        ),
                        "Accept": "text/html,application/xhtml+xml,application/xml,text/xml,text/plain,*/*;q=0.1",
                    },
                ) as response:
                    if response.status_code in _REDIRECT_STATUSES:
                        location = response.headers.get("location", "").strip()
                        if not location:
                            raise SiteFetchError(
                                "http_error",
                                "The page returned a redirect without a destination.",
                            )
                        if redirect_count >= self.max_redirects:
                            raise SiteFetchError(
                                "http_error",
                                "The page exceeded the redirect limit.",
                            )
                        current_url = urljoin(target.request_url, location)
                        continue

                    content_type = response.headers.get("content-type", "").strip()
                    media_type = content_type.split(";", 1)[0].strip().casefold()
                    if accepted_media_types is not None and media_type not in accepted_media_types:
                        raise SiteFetchError(
                            "content_unsupported",
                            "The page returned an unsupported content type.",
                        )
                    content_length = response.headers.get("content-length", "").strip()
                    if content_length.isdigit() and int(content_length) > max_bytes:
                        raise SiteFetchError(
                            "response_too_large",
                            "The page response exceeded the size limit.",
                        )

                    body = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=65_536):
                        body.extend(chunk)
                        if len(body) > max_bytes:
                            raise SiteFetchError(
                                "response_too_large",
                                "The page response exceeded the size limit.",
                            )
                    return SiteFetchResponse(
                        requested_url=initial_url,
                        final_url=str(response.url),
                        status_code=response.status_code,
                        content_type=content_type,
                        body=bytes(body),
                    )
        raise SiteFetchError("http_error", "The page exceeded the redirect limit.")
