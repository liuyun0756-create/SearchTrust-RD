"""One bounded Firecrawl scrape; returns observations, never absence conclusions."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
from urllib.parse import urlsplit

import httpx

from app.collectors.site_inventory_urls import SiteScope, canonicalize_site_resource_url
from app.security_v22.urls import Resolver, UrlSafetyError, UrlUnreachableError, resolve_public_url


@dataclass(frozen=True)
class FirecrawlRenderResult:
    html: str | None = None
    final_url: str | None = None
    status_code: int | None = None
    limitation: str | None = None


class FirecrawlRenderAdapter:
    def __init__(self, *, api_key: str, api_url: str, enabled: bool,
                 timeout_seconds: float = 30, max_response_bytes: int = 2_000_000,
                 http_client: httpx.AsyncClient | None = None, resolver: Resolver | None = None):
        self.api_key, self.api_url, self.enabled = api_key, api_url.rstrip('/'), enabled
        self.timeout_seconds, self.max_response_bytes = timeout_seconds, max_response_bytes
        self.http_client, self.resolver = http_client, resolver

    @property
    def available(self):
        return self.enabled and bool(self.api_key)

    async def scrape(self, url: str, *, scope: SiteScope) -> FirecrawlRenderResult:
        if not self.available:
            return FirecrawlRenderResult(limitation='render_unavailable')
        client = self.http_client
        owns_client = client is None
        try:
            async with asyncio.timeout(self.timeout_seconds):
                safe = await resolve_public_url(url, resolver=self.resolver)
                if not scope.contains_host(urlsplit(safe.normalized_url).hostname or ''):
                    return FirecrawlRenderResult(limitation='render_unavailable')
                client = client or httpx.AsyncClient(timeout=self.timeout_seconds, follow_redirects=False, trust_env=False)
                async with client.stream('POST', f'{self.api_url}/scrape',
                    headers={'Authorization': f'Bearer {self.api_key}', 'Content-Type': 'application/json'},
                    json={'url': url, 'formats': ['html'], 'onlyMainContent': False,
                          'maxAge': 0, 'waitFor': 1000, 'parsePDF': False, 'proxy': 'basic',
                          'timeout': max(1000, int((self.timeout_seconds - 2) * 1000))},
                    timeout=self.timeout_seconds) as response:
                    response.raise_for_status()
                    length = response.headers.get('content-length', '')
                    if length.isdigit() and int(length) > self.max_response_bytes:
                        raise ValueError('response too large')
                    body = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=65_536):
                        body.extend(chunk)
                        if len(body) > self.max_response_bytes:
                            raise ValueError('response too large')
                payload = json.loads(body)
                if not isinstance(payload, dict) or payload.get('success') is not True:
                    raise ValueError('invalid response')
                data = payload.get('data')
                if not isinstance(data, dict) or not isinstance(data.get('html'), str) or not data['html'].strip():
                    raise ValueError('HTML unavailable')
                metadata = data.get('metadata')
                if not isinstance(metadata, dict):
                    raise ValueError('metadata unavailable')
                status = metadata.get('statusCode')
                if type(status) is not int or not 200 <= status < 300:
                    raise ValueError('unsuccessful page')
                final = metadata.get('url') or metadata.get('sourceURL')
                if not isinstance(final, str) or canonicalize_site_resource_url(final, scope=scope) is None:
                    raise ValueError('invalid final URL')
                await resolve_public_url(final, resolver=self.resolver)
                return FirecrawlRenderResult(html=data['html'], final_url=final, status_code=status)
        except (httpx.HTTPError, TimeoutError, UrlSafetyError, UrlUnreachableError, ValueError, TypeError):
            return FirecrawlRenderResult(limitation='render_unavailable')
        finally:
            if owns_client and client is not None:
                await client.aclose()
