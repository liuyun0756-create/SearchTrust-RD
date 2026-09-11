"""Pure public-page signal extraction for v2.2 preflight."""

from __future__ import annotations

import html as html_module
import json
import re
from dataclasses import dataclass
from typing import Any, Literal

Confidence = Literal["low", "medium", "high"]


@dataclass(frozen=True)
class TextSignal:
    value: str
    source: str
    confidence: Confidence


@dataclass(frozen=True)
class MarketSignal:
    display_name: str
    country_code: str
    region: str | None
    city: str | None
    postal_code: str | None
    source: str
    confidence: Confidence


@dataclass(frozen=True)
class SiteSignals:
    names: tuple[TextSignal, ...]
    phones: tuple[TextSignal, ...]
    services: tuple[TextSignal, ...]
    markets: tuple[MarketSignal, ...]
    operating_models: tuple[TextSignal, ...]
    gbp_url: str | None


_COUNTRY_CODES = {
    "us": "US",
    "usa": "US",
    "united states": "US",
    "united states of america": "US",
    "ca": "CA",
    "canada": "CA",
    "gb": "GB",
    "uk": "GB",
    "united kingdom": "GB",
    "au": "AU",
    "australia": "AU",
}
_GENERIC_SERVICES = {
    "home",
    "services",
    "professional service",
    "local business",
    "organization",
}


def _visible_identity_signals(document: str) -> tuple[list[TextSignal], list[TextSignal]]:
    names: list[TextSignal] = []
    phones: list[TextSignal] = []
    meta_patterns = (
        r'<meta[^>]+property=["\']og:site_name["\'][^>]+content=["\']([^"\']+)',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:site_name["\']',
    )
    for pattern in meta_patterns:
        match = re.search(pattern, document, re.IGNORECASE)
        if match:
            value = html_module.unescape(match.group(1)).strip()
            is_domain = bool(re.fullmatch(
                r"(?:www\.)?(?:[a-z0-9-]+\.)+[a-z]{2,}",
                value.casefold(),
            ))
            if 1 <= len(value) <= 240:
                names.append(TextSignal(
                    value,
                    "og_site_name",
                    "low" if is_domain else "medium",
                ))
            break
    for match in re.findall(
        r"(?:tel:|phone[:：]?\s*|call[:：]?\s*)(\+?[\d\s().-]{7,22})",
        document,
        re.IGNORECASE,
    ):
        value = " ".join(match.split()).strip()
        if len(re.sub(r"\D", "", value)) >= 10:
            phones.append(TextSignal(value, "visible_phone", "medium"))
    return names, phones


def _maps_url(document: str) -> str | None:
    decoded = html_module.unescape(document or "")
    patterns = (
        r'https://(?:www\.)?google\.com/maps/[^\s\'"<>]*0x[0-9a-fA-F]+:0x[0-9a-fA-F]+[^\s\'"<>]*',
        r'https://(?:www\.)?google\.com/maps/(?:dir|search|place)/[^\s\'"<>]*(?:destination_place_id|query_place_id|place_id)=[^\s\'"<>&]+[^\s\'"<>]*',
        r'https://search\.google\.com/local/reviews\?[^\s\'"<>]*placeid=[^\s\'"<>&]+[^\s\'"<>]*',
        r'https://maps\.app\.goo\.gl/[^\s\'"<>\)\]]+',
        r'https://goo\.gl/maps/[^\s\'"<>\)\]]+',
        r'https://share\.google/[^\s\'"<>\)\]]+',
    )
    for pattern in patterns:
        match = re.search(pattern, decoded, re.IGNORECASE)
        if match:
            return match.group(0).rstrip("),.;]")
    return None


def _records(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [record for item in value for record in _records(item)]
    if not isinstance(value, dict):
        return []
    records = [value]
    graph = value.get("@graph")
    if isinstance(graph, list):
        records.extend(record for item in graph for record in _records(item))
    return records


def _json_ld_records(html: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for raw in re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        try:
            records.extend(_records(json.loads(html_module.unescape(raw).strip())))
        except (json.JSONDecodeError, TypeError):
            continue
    return records


def _dedupe_text(signals: list[TextSignal]) -> tuple[TextSignal, ...]:
    result: list[TextSignal] = []
    seen: set[str] = set()
    for signal in signals:
        key = re.sub(r"\s+", " ", signal.value).strip().casefold()
        if key and key not in seen:
            seen.add(key)
            result.append(TextSignal(re.sub(r"\s+", " ", signal.value).strip(), signal.source, signal.confidence))
    return tuple(result)


def _country_code(value: Any) -> str | None:
    text = str(value or "").strip().casefold()
    if len(text) == 2 and text.isalpha():
        return text.upper()
    return _COUNTRY_CODES.get(text)


def _service_values(record: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for key in ("serviceType", "category"):
        raw = record.get(key)
        items = raw if isinstance(raw, list) else [raw]
        values.extend(str(item).strip() for item in items if str(item or "").strip())
    offers = record.get("makesOffer") or record.get("hasOfferCatalog")
    for offer in _records(offers):
        item = offer.get("itemOffered") if isinstance(offer.get("itemOffered"), dict) else offer
        for key in ("serviceType", "name"):
            value = str(item.get(key) or "").strip()
            if value:
                values.append(value)
    return values


def extract_site_signals(html: str) -> SiteSignals:
    """Extract provenance-bearing candidates without filling missing facts."""

    names: list[TextSignal] = []
    phones: list[TextSignal] = []
    services: list[TextSignal] = []
    markets: list[MarketSignal] = []
    has_address = False
    has_service_area = False

    records = _json_ld_records(html)
    for record in records:
        record_types = record.get("@type")
        types = record_types if isinstance(record_types, list) else [record_types]
        normalized_types = {str(value).strip().casefold() for value in types if str(value or "").strip()}
        is_business = bool(
            normalized_types.intersection({"localbusiness", "organization", "plumber", "professionalservice"})
            or any(value.endswith("business") for value in normalized_types)
        )
        if is_business:
            name = str(record.get("name") or "").strip()
            phone = str(record.get("telephone") or "").strip()
            if 1 <= len(name) <= 240:
                names.append(TextSignal(name, "json_ld", "high"))
            if phone:
                phones.append(TextSignal(phone, "json_ld", "high"))

        address = record.get("address")
        if isinstance(address, dict):
            city = str(address.get("addressLocality") or "").strip() or None
            region = str(address.get("addressRegion") or "").strip() or None
            postal = str(address.get("postalCode") or "").strip() or None
            country = _country_code(address.get("addressCountry"))
            street = str(address.get("streetAddress") or "").strip()
            has_address = has_address or bool(street)
            fields_fit = (
                (city is None or len(city) <= 120)
                and (region is None or len(region) <= 120)
                and (postal is None or len(postal) <= 32)
            )
            if country and (city or region or postal) and fields_fit:
                display = ", ".join(value for value in (city, region, country) if value)
                if len(display) <= 200:
                    markets.append(MarketSignal(display, country, region, city, postal, "json_ld", "high"))

        area_served = record.get("areaServed") or record.get("serviceArea")
        if area_served:
            has_service_area = True

        for value in _service_values(record):
            if value.casefold() not in _GENERIC_SERVICES and len(value) <= 200:
                services.append(TextSignal(value, "json_ld", "high"))

    visible_names, visible_phones = _visible_identity_signals(html)
    names.extend(visible_names)
    phones.extend(visible_phones)

    operating_models: list[TextSignal] = []
    if has_address and has_service_area:
        operating_models.append(TextSignal("hybrid", "json_ld", "high"))
    elif has_address:
        operating_models.append(TextSignal("storefront", "json_ld", "high"))
    elif has_service_area:
        operating_models.append(TextSignal("service_area", "json_ld", "high"))

    return SiteSignals(
        names=_dedupe_text(names),
        phones=_dedupe_text(phones),
        services=_dedupe_text(services),
        markets=tuple(dict.fromkeys(markets)),
        operating_models=_dedupe_text(operating_models),
        gbp_url=_maps_url(html),
    )
