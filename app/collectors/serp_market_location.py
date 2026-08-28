"""Deterministic single-point resolution for v2.2 SERP collection."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

import httpx

from app.collectors.serp_market_models import SerpTargetPoint
from app.report_v22.models import TargetMarket


class SerpLocationResolutionError(RuntimeError):
    def __init__(self, code: str, user_message: str, *, retryable: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.user_message = user_message
        self.retryable = retryable


@dataclass(frozen=True)
class LocationLookupResponse:
    items: tuple[dict[str, Any], ...]
    response_checksum: str


class LocationProvider(Protocol):
    async def search(self, query: str) -> LocationLookupResponse: ...


def _normalize(value: str | None) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or "")).casefold()
    return re.sub(r"[^a-z0-9]+", " ", normalized).strip()


def build_location_query(market: TargetMarket) -> str:
    values = [
        market.postal_code,
        market.city,
        market.region,
        market.display_name,
        market.country_code,
    ]
    parts: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        normalized = _normalize(text)
        if text and normalized not in seen:
            seen.add(normalized)
            parts.append(text)
    return ", ".join(parts)[:300]


def _candidate_score(item: dict[str, Any], market: TargetMarket) -> int | None:
    if str(item.get("country_code") or "").upper() != market.country_code:
        return None
    name = str(item.get("name") or "").strip()
    canonical = str(item.get("canonical_name") or "").strip()
    candidate_text = _normalize(f"{name} {canonical}")
    if not name or not canonical or not candidate_text:
        return None

    postal = _normalize(market.postal_code)
    city = _normalize(market.city)
    region = _normalize(market.region)
    display = _normalize(market.display_name)
    if postal and postal not in candidate_text:
        return None
    if city and city not in candidate_text:
        return None

    score = 0
    if display and display == _normalize(name):
        score += 80
    if display and display == _normalize(canonical):
        score += 100
    if postal:
        score += 100
    if city:
        score += 50
    if region and region in candidate_text:
        score += 25
    display_tokens = set(display.split())
    score += len(display_tokens.intersection(candidate_text.split()))
    target_type = _normalize(str(item.get("target_type") or ""))
    if postal and "postal" in target_type:
        score += 40
    elif city and target_type == "city":
        score += 40
    return score


def _coordinates(item: dict[str, Any]) -> tuple[float, float] | None:
    gps = item.get("gps")
    if not isinstance(gps, list) or len(gps) != 2:
        return None
    longitude, latitude = gps
    if isinstance(longitude, bool) or isinstance(latitude, bool):
        return None
    if not isinstance(longitude, (int, float)) or not isinstance(latitude, (int, float)):
        return None
    if not -180 <= longitude <= 180 or not -90 <= latitude <= 90:
        return None
    return float(latitude), float(longitude)


async def resolve_target_point(
    market: TargetMarket,
    *,
    provider: LocationProvider | None,
    clock: Callable[[], datetime] | None = None,
) -> SerpTargetPoint:
    now = (clock or (lambda: datetime.now(timezone.utc)))()
    has_latitude = market.latitude is not None
    has_longitude = market.longitude is not None
    if has_latitude != has_longitude:
        raise SerpLocationResolutionError(
            "SERP_LOCATION_COORDINATES_INCOMPLETE",
            "The target market must provide both latitude and longitude.",
        )
    if has_latitude and has_longitude:
        return SerpTargetPoint(
            requested_label=market.display_name,
            canonical_name=market.display_name,
            country_code=market.country_code,
            latitude=market.latitude,
            longitude=market.longitude,
            source="explicit_coordinates",
            resolved_at=now,
        )
    if provider is None:
        raise SerpLocationResolutionError(
            "SERP_LOCATION_PROVIDER_UNAVAILABLE",
            "The target market could not be resolved to one search point.",
            retryable=True,
        )

    lookup = await provider.search(build_location_query(market))
    scored: list[tuple[int, str, dict[str, Any], tuple[float, float]]] = []
    for item in lookup.items[:10]:
        if not isinstance(item, dict):
            continue
        score = _candidate_score(item, market)
        coordinates = _coordinates(item)
        provider_id = str(item.get("id") or "").strip()
        name = str(item.get("name") or "").strip()
        canonical = str(item.get("canonical_name") or "").strip()
        if (
            score is None
            or coordinates is None
            or not provider_id
            or len(provider_id) > 200
            or len(name) > 200
            or len(canonical) > 300
        ):
            continue
        scored.append((score, provider_id, item, coordinates))
    if not scored:
        raise SerpLocationResolutionError(
            "SERP_LOCATION_NOT_FOUND",
            "The target market could not be matched to a supported search location.",
        )
    scored.sort(key=lambda row: (-row[0], row[1]))
    highest = scored[0][0]
    finalists = [row for row in scored if row[0] == highest]
    distinct_points = {
        (
            str(row[2].get("canonical_name") or "").strip(),
            row[3],
        )
        for row in finalists
    }
    if len(distinct_points) != 1:
        raise SerpLocationResolutionError(
            "SERP_LOCATION_AMBIGUOUS",
            "The target market matches more than one supported search location.",
        )
    _, provider_id, selected, (latitude, longitude) = finalists[0]
    canonical_name = str(selected.get("canonical_name") or "").strip()
    return SerpTargetPoint(
        requested_label=market.display_name,
        canonical_name=canonical_name,
        country_code=market.country_code,
        latitude=latitude,
        longitude=longitude,
        source="serpapi_location",
        provider_location_id=provider_id,
        resolved_at=now,
        response_checksum=lookup.response_checksum,
    )


class SerpApiLocationProvider:
    def __init__(
        self,
        *,
        url: str,
        connect_timeout: int,
        read_timeout: int,
        total_timeout: int,
        max_response_bytes: int,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
    ) -> None:
        self.url = url
        self.timeout = httpx.Timeout(
            connect=connect_timeout,
            read=read_timeout,
            write=read_timeout,
            pool=connect_timeout,
        )
        self.total_timeout = total_timeout
        self.max_response_bytes = max_response_bytes
        self.client_factory = client_factory or self._default_client

    def _default_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=self.timeout,
            follow_redirects=False,
            trust_env=False,
        )

    async def search(self, query: str) -> LocationLookupResponse:
        try:
            async with asyncio.timeout(self.total_timeout):
                async with self.client_factory() as client:
                    async with client.stream(
                        "GET",
                        self.url,
                        params={"q": query, "limit": "10"},
                        headers={"Accept": "application/json"},
                    ) as response:
                        if response.status_code == 429 or response.status_code >= 500:
                            raise SerpLocationResolutionError(
                                "SERP_LOCATION_PROVIDER_TEMPORARY",
                                "The search location provider is temporarily unavailable.",
                                retryable=True,
                            )
                        if response.status_code >= 400:
                            raise SerpLocationResolutionError(
                                "SERP_LOCATION_REQUEST_REJECTED",
                                "The search location provider rejected the target market.",
                            )
                        chunks: list[bytes] = []
                        size = 0
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            if size > self.max_response_bytes:
                                raise SerpLocationResolutionError(
                                    "SERP_LOCATION_RESPONSE_TOO_LARGE",
                                    "The search location response exceeded the safe limit.",
                                )
                            chunks.append(chunk)
        except SerpLocationResolutionError:
            raise
        except (TimeoutError, httpx.TimeoutException, httpx.NetworkError) as exc:
            raise SerpLocationResolutionError(
                "SERP_LOCATION_PROVIDER_TEMPORARY",
                "The search location provider is temporarily unavailable.",
                retryable=True,
            ) from exc
        raw = b"".join(chunks)
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SerpLocationResolutionError(
                "SERP_LOCATION_RESPONSE_INVALID",
                "The search location provider returned an invalid response.",
            ) from exc
        if not isinstance(payload, list):
            raise SerpLocationResolutionError(
                "SERP_LOCATION_RESPONSE_INVALID",
                "The search location provider returned an invalid response.",
            )
        return LocationLookupResponse(
            items=tuple(item for item in payload[:10] if isinstance(item, dict)),
            response_checksum=f"sha256:{hashlib.sha256(raw).hexdigest()}",
        )
