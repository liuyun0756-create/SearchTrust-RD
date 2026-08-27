"""Bounded homepage collection with DNS-pinned connections."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

import httpcore
import httpx

from app.preflight_v22.urls import Resolver, SafeUrl, normalize_site_url, resolve_public_url


ClientFactory = Callable[[SafeUrl], httpx.AsyncClient]

_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_HTML_MEDIA_TYPES = {"text/html", "application/xhtml+xml"}


class HomepageFetchError(OSError):
    """A safe public page could not produce a bounded HTML snapshot."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.user_message = message


@dataclass(frozen=True)
class HomepageSnapshot:
    source_url: str
    normalized_site_url: str
    status_code: int
    html: str


class _PinnedNetworkBackend(httpcore.AsyncNetworkBackend):
    """Connect to prevalidated addresses while httpcore retains Host/SNI."""

    def __init__(
        self,
        target: SafeUrl,
        *,
        delegate: httpcore.AsyncNetworkBackend | None = None,
    ) -> None:
        self.target = target
        self.delegate = delegate or httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        if host.rstrip(".").lower() != self.target.hostname:
            raise httpcore.ConnectError("The connection host did not match the validated target.")
        last_error: Exception | None = None
        for address in self.target.addresses:
            try:
                return await self.delegate.connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except Exception as exc:  # httpcore backends expose transport-specific subclasses.
                last_error = exc
        if last_error is not None:
            raise last_error
        raise httpcore.ConnectError("No validated address was available.")

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        raise httpcore.ConnectError("Unix sockets are not allowed for preflight requests.")

    async def sleep(self, seconds: float) -> None:
        await self.delegate.sleep(seconds)


class _PinnedAsyncHTTPTransport(httpx.AsyncHTTPTransport):
    def __init__(self, target: SafeUrl) -> None:
        super().__init__(trust_env=False, retries=0, http1=True, http2=False)
        self._pool._network_backend = _PinnedNetworkBackend(target)  # type: ignore[attr-defined]


class BoundedHomepageFetcher:
    def __init__(
        self,
        *,
        connect_timeout: float,
        read_timeout: float,
        total_timeout: float,
        max_redirects: int,
        max_response_bytes: int,
        resolver: Resolver | None = None,
        client_factory: ClientFactory | None = None,
    ) -> None:
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.total_timeout = total_timeout
        self.max_redirects = max_redirects
        self.max_response_bytes = max_response_bytes
        self.resolver = resolver
        self.client_factory = client_factory or self._default_client

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

    async def fetch(self, site_url: str) -> HomepageSnapshot:
        root_url = normalize_site_url(site_url)
        try:
            return await asyncio.wait_for(self._fetch_redirects(root_url), timeout=self.total_timeout)
        except TimeoutError as exc:
            raise HomepageFetchError(
                "SITE_TIMEOUT",
                "The site did not respond within the preflight time limit.",
            ) from exc
        except httpx.TimeoutException as exc:
            raise HomepageFetchError(
                "SITE_TIMEOUT",
                "The site did not respond within the preflight time limit.",
            ) from exc
        except httpx.HTTPError as exc:
            raise HomepageFetchError(
                "SITE_UNREACHABLE",
                "The site could not be reached during preflight.",
            ) from exc

    async def _fetch_redirects(self, initial_url: str) -> HomepageSnapshot:
        current_url = initial_url
        for redirect_count in range(self.max_redirects + 1):
            target = await resolve_public_url(current_url, resolver=self.resolver)
            async with self.client_factory(target) as client:
                async with client.stream(
                    "GET",
                    target.request_url,
                    headers={
                        "User-Agent": "SearchTrust-Preflight/2.2 (+https://trysearchtrust.com)",
                        "Accept": "text/html,application/xhtml+xml",
                    },
                ) as response:
                    if response.status_code in _REDIRECT_STATUSES:
                        location = response.headers.get("location", "").strip()
                        if not location:
                            raise HomepageFetchError(
                                "SITE_REDIRECT_INVALID",
                                "The site returned a redirect without a valid destination.",
                            )
                        if redirect_count >= self.max_redirects:
                            raise HomepageFetchError(
                                "SITE_REDIRECT_LIMIT",
                                "The site exceeded the preflight redirect limit.",
                            )
                        current_url = urljoin(target.request_url, location)
                        continue

                    if response.status_code >= 400:
                        raise HomepageFetchError(
                            "SITE_HTTP_ERROR",
                            "The site returned an error response during preflight.",
                        )
                    media_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                    if media_type not in _HTML_MEDIA_TYPES:
                        raise HomepageFetchError(
                            "SITE_CONTENT_UNSUPPORTED",
                            "The site did not return an HTML page.",
                        )

                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > self.max_response_bytes:
                            raise HomepageFetchError(
                                "SITE_RESPONSE_TOO_LARGE",
                                "The site response exceeded the preflight size limit.",
                            )
                    encoding = response.encoding or "utf-8"
                    try:
                        html = bytes(body).decode(encoding, errors="replace")
                    except LookupError:
                        html = bytes(body).decode("utf-8", errors="replace")
                    return HomepageSnapshot(
                        source_url=str(response.url),
                        normalized_site_url=target.normalized_url,
                        status_code=response.status_code,
                        html=html,
                    )
        raise HomepageFetchError("SITE_REDIRECT_LIMIT", "The site exceeded the redirect limit.")
