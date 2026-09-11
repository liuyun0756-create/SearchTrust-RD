"""One-call public GBP discovery adapter for v2.2 preflight."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urljoin, urlsplit

import httpx

from app.report_common.google_business import (
    configured_keys,
    data_cid,
    extract_data_id,
    extract_google_place_id,
    request_serpapi,
)
from app.report_v22.models import OperatingModel, TargetMarket
from app.preflight_v22.extractors import SiteSignals
from app.security_v22.http import PinnedAsyncHTTPTransport
from app.security_v22.urls import Resolver, SafeUrl, resolve_public_url, validate_gbp_url


Provider = Callable[[dict[str, str]], Awaitable[dict[str, Any]]]
UrlExpander = Callable[[str], Awaitable[str]]
LookupStatus = Literal["found", "not_found", "unavailable"]


@dataclass(frozen=True)
class GbpCandidate:
    business_name: str
    website_url: str | None
    public_gbp_url: str | None
    phone: str | None
    address: str | None
    market: TargetMarket | None
    operating_model: OperatingModel | None
    categories: tuple[str, ...]


@dataclass(frozen=True)
class GbpLookupResult:
    status: LookupStatus
    code: str
    message: str
    candidates: tuple[GbpCandidate, ...]


_US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS",
    "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
    "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
    "WI", "WY", "DC",
}
_SHORT_GBP_HOSTS = {"maps.app.goo.gl", "goo.gl", "share.google"}


class GoogleMapsUrlExpander:
    """Expand approved Google short links without following an unsafe hop."""

    def __init__(
        self,
        *,
        connect_timeout: float = 5,
        read_timeout: float = 10,
        max_redirects: int = 3,
        resolver: Resolver | None = None,
        client_factory: Callable[[SafeUrl], httpx.AsyncClient] | None = None,
    ) -> None:
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.max_redirects = max_redirects
        self.resolver = resolver
        self.client_factory = client_factory or self._default_client

    def _default_client(self, target: SafeUrl) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=PinnedAsyncHTTPTransport(target),
            timeout=httpx.Timeout(
                connect=self.connect_timeout,
                read=self.read_timeout,
                write=self.read_timeout,
                pool=self.connect_timeout,
            ),
            follow_redirects=False,
            trust_env=False,
        )

    async def expand(self, value: str) -> str:
        current = validate_gbp_url(value)
        if (urlsplit(current).hostname or "").lower() not in _SHORT_GBP_HOSTS:
            return current
        for redirect_count in range(self.max_redirects + 1):
            target = await resolve_public_url(current, resolver=self.resolver)
            async with self.client_factory(target) as client:
                async with client.stream(
                    "GET",
                    target.request_url,
                    headers={
                        "User-Agent": "SearchTrust-Preflight/2.2 (+https://trysearchtrust.com)",
                        "Accept": "text/html",
                    },
                ) as response:
                    location = response.headers.get("location", "").strip()
            if response.status_code not in {301, 302, 303, 307, 308} or not location:
                return current
            if redirect_count >= self.max_redirects:
                return current
            next_url = validate_gbp_url(urljoin(target.request_url, location))
            if (urlsplit(next_url).hostname or "").lower() not in _SHORT_GBP_HOSTS:
                return next_url
            current = next_url
        return current


def _market_from_result(result: dict[str, Any]) -> TargetMarket | None:
    address = str(result.get("address") or "").strip()
    match = re.search(
        r",\s*([^,]{2,120}?),\s*([A-Z]{2})\s+(\d{5}(?:-\d{4})?)(?:,\s*(?:US|USA))?$",
        address,
    )
    if not match or match.group(2) not in _US_STATES:
        return None
    city, region, postal_code = (value.strip() for value in match.groups())
    coordinates = result.get("gps_coordinates")
    latitude = coordinates.get("latitude") if isinstance(coordinates, dict) else None
    longitude = coordinates.get("longitude") if isinstance(coordinates, dict) else None
    latitude_value = float(latitude) if isinstance(latitude, (int, float)) and -90 <= latitude <= 90 else None
    longitude_value = (
        float(longitude) if isinstance(longitude, (int, float)) and -180 <= longitude <= 180 else None
    )
    return TargetMarket(
        display_name=f"{city}, {region}, US",
        country_code="US",
        region=region,
        city=city,
        postal_code=postal_code,
        latitude=latitude_value,
        longitude=longitude_value,
    )


def _public_gbp_url(result: dict[str, Any], fallback: str | None) -> str | None:
    data_id = str(result.get("data_id") or "").strip()
    cid = data_cid(data_id)
    if cid:
        return f"https://www.google.com/maps?cid={cid}"
    return fallback


def _normalize_candidate(result: dict[str, Any], fallback_gbp_url: str | None) -> GbpCandidate | None:
    name = str(result.get("title") or result.get("name") or "").strip()
    if not name or len(name) > 240:
        return None
    website = str(result.get("website") or "").strip() or None
    if website and len(website) <= 2083:
        try:
            parsed = urlsplit(website)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                website = None
        except ValueError:
            website = None
    elif website:
        website = None
    address = str(result.get("address") or "").strip() or None
    phone = str(result.get("phone") or "").strip() or None
    category_value = result.get("types") or result.get("categories") or result.get("type")
    raw_categories = category_value if isinstance(category_value, list) else [category_value]
    categories = tuple(
        dict.fromkeys(
            str(item).strip()
            for item in raw_categories
            if str(item or "").strip() and len(str(item).strip()) <= 240
        )
    )
    service_area = result.get("service_area_business")
    if service_area is None:
        service_area = result.get("pure_service_area_business")
    if bool(service_area) and address:
        operating_model: OperatingModel | None = "hybrid"
    elif bool(service_area):
        operating_model = "service_area"
    elif address:
        operating_model = "storefront"
    else:
        operating_model = None
    return GbpCandidate(
        business_name=name,
        website_url=website,
        public_gbp_url=_public_gbp_url(result, fallback_gbp_url),
        phone=phone,
        address=address,
        market=_market_from_result(result),
        operating_model=operating_model,
        categories=categories,
    )


class LimitedGbpLookup:
    def __init__(
        self,
        *,
        provider: Provider | None = None,
        configured: bool | None = None,
        url_expander: UrlExpander | None = None,
    ) -> None:
        self.provider = provider or self._default_provider
        self.configured = bool(configured_keys()) if configured is None else configured
        self.url_expander = url_expander or GoogleMapsUrlExpander().expand

    async def _default_provider(self, params: dict[str, str]) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0), follow_redirects=False) as client:
            return await request_serpapi(client, params)

    def _request_params(
        self,
        *,
        site_url: str,
        signals: SiteSignals,
        gbp_url: str | None,
    ) -> dict[str, str]:
        place_id = extract_google_place_id(gbp_url or "")
        raw_data_id = extract_data_id(gbp_url or "")
        cid = data_cid(raw_data_id)
        params = {"engine": "google_maps", "hl": "en"}
        if place_id:
            params["place_id"] = place_id
            return params
        if cid:
            params["data_cid"] = cid
            return params

        domain = (urlsplit(site_url).hostname or "").removeprefix("www.")
        query_parts = [domain]
        if signals.names:
            query_parts.append(signals.names[0].value)
        if signals.markets:
            query_parts.append(signals.markets[0].display_name)
        params.update({"q": " ".join(part for part in query_parts if part), "type": "search"})
        return params

    async def lookup(
        self,
        *,
        site_url: str,
        signals: SiteSignals,
        gbp_url: str | None = None,
    ) -> GbpLookupResult:
        if not self.configured:
            return GbpLookupResult(
                "unavailable",
                "GBP_LOOKUP_UNAVAILABLE",
                "Public GBP lookup is not configured.",
                (),
            )
        resolved_gbp_url = gbp_url
        if gbp_url and (urlsplit(gbp_url).hostname or "").lower() in _SHORT_GBP_HOSTS:
            try:
                resolved_gbp_url = await self.url_expander(gbp_url)
            except Exception:
                resolved_gbp_url = gbp_url
        params = self._request_params(
            site_url=site_url,
            signals=signals,
            gbp_url=resolved_gbp_url,
        )
        try:
            payload = await self.provider(params)
        except Exception:  # provider details are intentionally not exposed.
            return GbpLookupResult(
                "unavailable",
                "GBP_LOOKUP_UNAVAILABLE",
                "Public GBP lookup is temporarily unavailable.",
                (),
            )
        if not isinstance(payload, dict) or payload.get("error"):
            return GbpLookupResult(
                "unavailable",
                "GBP_LOOKUP_UNAVAILABLE",
                "Public GBP lookup is temporarily unavailable.",
                (),
            )

        raw_candidates: list[dict[str, Any]] = []
        place = payload.get("place_results")
        if isinstance(place, dict):
            raw_candidates.append(place)
        local = payload.get("local_results")
        if isinstance(local, dict):
            raw_candidates.append(local)
        elif isinstance(local, list):
            raw_candidates.extend(item for item in local if isinstance(item, dict))
        normalized_candidates: list[GbpCandidate] = []
        for item in raw_candidates[:5]:
            try:
                candidate = _normalize_candidate(item, resolved_gbp_url)
            except (TypeError, ValueError):
                candidate = None
            if candidate is not None:
                normalized_candidates.append(candidate)
        candidates = tuple(normalized_candidates)
        if not candidates:
            return GbpLookupResult(
                "not_found",
                "GBP_NOT_FOUND",
                "No public GBP candidate was found.",
                (),
            )
        return GbpLookupResult("found", "GBP_FOUND", "Public GBP candidates were found.", candidates)
