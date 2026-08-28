"""Single-call Firecrawl Map adapter for supplemental inventory discovery."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx


@dataclass(frozen=True)
class FirecrawlMapResult:
    urls: tuple[str, ...]
    limitation: str | None


class FirecrawlMapAdapter:
    def __init__(
        self,
        *,
        api_key: str,
        api_url: str,
        enabled: bool,
        timeout_seconds: float,
        http_client: httpx.AsyncClient | None = None,
        max_response_bytes: int = 1_000_000,
    ) -> None:
        self.api_key = api_key
        self.api_url = api_url.rstrip("/")
        self.enabled = enabled
        self.timeout_seconds = timeout_seconds
        self.http_client = http_client
        self.max_response_bytes = max_response_bytes

    async def map(self, root_url: str, *, limit: int) -> FirecrawlMapResult:
        if not self.enabled or not self.api_key or limit <= 0:
            return FirecrawlMapResult((), "firecrawl_unavailable")

        owns_client = self.http_client is None
        client = self.http_client or httpx.AsyncClient(
            timeout=self.timeout_seconds,
            follow_redirects=False,
            trust_env=False,
        )
        try:
            async with client.stream(
                "POST",
                f"{self.api_url}/map",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={"url": root_url, "limit": min(limit, 500)},
                timeout=self.timeout_seconds,
            ) as response:
                response.raise_for_status()
                content_length = response.headers.get("content-length", "")
                if content_length.isdigit() and int(content_length) > self.max_response_bytes:
                    raise ValueError("Firecrawl response exceeded the size limit")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > self.max_response_bytes:
                        raise ValueError("Firecrawl response exceeded the size limit")
            payload: Any = json.loads(body)
            if not isinstance(payload, dict) or payload.get("success") is not True:
                raise ValueError("Firecrawl returned an invalid result")
            raw_links = payload.get("links")
            if not isinstance(raw_links, list):
                raise ValueError("Firecrawl links were missing")
            urls: list[str] = []
            for item in raw_links:
                value = item if isinstance(item, str) else item.get("url") if isinstance(item, dict) else None
                if isinstance(value, str) and value.strip() and value not in urls:
                    urls.append(value.strip())
                if len(urls) >= min(limit, 500):
                    break
            return FirecrawlMapResult(tuple(urls), None)
        except (httpx.HTTPError, json.JSONDecodeError, TypeError, ValueError):
            return FirecrawlMapResult((), "firecrawl_unavailable")
        finally:
            if owns_client:
                await client.aclose()
