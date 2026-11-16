"""Single-call Firecrawl Map adapter for supplemental inventory discovery."""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from time import monotonic
from typing import Any
from uuid import UUID

import httpx


logger = logging.getLogger(__name__)
_job_id: ContextVar[UUID | None] = ContextVar("firecrawl_map_job_id", default=None)


@contextmanager
def firecrawl_job_context(job_id: UUID):
    """Attach safe job identity without changing mapper/checkpoint contracts."""
    token = _job_id.set(job_id)
    try:
        yield
    finally:
        _job_id.reset(token)


def _map_endpoint(api_url: str) -> str:
    """Map uses v2; the shared base may still serve the existing v1 scraper."""
    base = api_url.rstrip("/")
    if base.rsplit("/", 1)[-1] in {"v1", "v2"}:
        base = base.rsplit("/", 1)[0]
    return f"{base}/v2/map"


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
        started = monotonic()
        stage = "disabled"
        status: int | None = None
        exception_type: str | None = None
        warnings_count = 0
        owns_client = False
        client = self.http_client
        try:
            if not self.enabled or not self.api_key or limit <= 0:
                return FirecrawlMapResult((), "firecrawl_unavailable")
            stage = "network"
            owns_client = client is None
            client = client or httpx.AsyncClient(
                timeout=self.timeout_seconds,
                follow_redirects=False,
                trust_env=False,
            )
            async with client.stream(
                "POST",
                _map_endpoint(self.api_url),
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={"url": root_url, "limit": min(limit, 500)},
                timeout=self.timeout_seconds,
            ) as response:
                status = response.status_code
                response.raise_for_status()
                content_length = response.headers.get("content-length", "")
                if content_length.isdigit() and int(content_length) > self.max_response_bytes:
                    raise ValueError("Firecrawl response exceeded the size limit")
                body = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=65_536):
                    body.extend(chunk)
                    if len(body) > self.max_response_bytes:
                        raise ValueError("Firecrawl response exceeded the size limit")
            stage = "json"
            payload: Any = json.loads(body)
            stage = "schema"
            if not isinstance(payload, dict) or payload.get("success") is not True:
                raise ValueError("Firecrawl returned an invalid result")
            if set(payload) - {"success", "links", "warnings", "replacement"}:
                raise ValueError("Firecrawl returned unknown fields")
            warnings = payload.get("warnings", [])
            if not isinstance(warnings, list) or any(not isinstance(item, str) for item in warnings):
                raise ValueError("Firecrawl returned invalid warnings")
            warnings_count = len(warnings)
            if "replacement" in payload and not isinstance(payload["replacement"], str):
                raise ValueError("Firecrawl returned an invalid replacement")
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
            stage = "none"
            return FirecrawlMapResult(tuple(urls), None)
        except (httpx.HTTPError, json.JSONDecodeError, TypeError, ValueError) as exc:
            exception_type = type(exc).__name__
            if isinstance(exc, httpx.HTTPStatusError):
                stage = "http"
            elif isinstance(exc, httpx.HTTPError):
                stage = "network"
            elif isinstance(exc, json.JSONDecodeError):
                stage = "json"
            else:
                stage = "schema"
            return FirecrawlMapResult((), "firecrawl_unavailable")
        finally:
            job_id = _job_id.get()
            logger.info(
                "firecrawl_map failure_stage=%s http_status=%s exception_type=%s "
                "job_id_suffix=%s elapsed_ms=%d warnings_count=%d",
                stage, status, exception_type, str(job_id)[-8:] if job_id else "unknown",
                max(int((monotonic() - started) * 1000), 0), warnings_count,
            )
            if owns_client and client is not None:
                await client.aclose()
