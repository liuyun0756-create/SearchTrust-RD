"""Version-neutral Google Business URL and SerpAPI helpers."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from app.core.config import settings
from app.integrations.serpapi import SerpApiKeyState, configured_serpapi_keys, execute_serpapi_get


_ACTIVE_KEY_FINGERPRINT = ""
_KEY_BLOCKED_UNTIL: dict[str, float] = {}


def configured_keys() -> list[str]:
    return configured_serpapi_keys(
        settings.SERPAPI_KEY,
        settings.SERPAPI_KEY_SECONDARY,
        settings.SERPAPI_KEY_TERTIARY,
    )


async def request_serpapi(client: httpx.AsyncClient, params: dict[str, str]) -> dict[str, Any]:
    global _ACTIVE_KEY_FINGERPRINT

    state = SerpApiKeyState(
        active_fingerprint=_ACTIVE_KEY_FINGERPRINT,
        blocked_until=_KEY_BLOCKED_UNTIL,
    )
    try:
        result = await execute_serpapi_get(
            client,
            params,
            keys=configured_keys(),
            base_url=settings.SERPAPI_BASE_URL,
            state=state,
        )
        return result.payload
    finally:
        _ACTIVE_KEY_FINGERPRINT = state.active_fingerprint


def extract_google_place_id(gbp_url: str) -> str | None:
    if not gbp_url:
        return None
    try:
        query = parse_qs(urlparse(gbp_url).query)
    except ValueError:
        return None
    for key in ("destination_place_id", "query_place_id", "place_id", "placeid"):
        values = query.get(key) or []
        if values and values[0].strip():
            return values[0].strip()
    return None


def extract_data_id(gbp_url: str) -> str | None:
    match = re.search(r"(0x[0-9a-fA-F]+:0x[0-9a-fA-F]+)", unquote(gbp_url or ""))
    return match.group(1) if match else None


def data_cid(data_id: str | None) -> str | None:
    if not data_id or ":" not in data_id:
        return None
    try:
        return str(int(data_id.rsplit(":", 1)[1], 16))
    except ValueError:
        return None

