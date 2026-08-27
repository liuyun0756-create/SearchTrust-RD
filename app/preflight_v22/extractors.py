"""Pure public-page signal extraction for v2.2 preflight."""

from __future__ import annotations

import html as html_module
import json
import re
from dataclasses import dataclass
from typing import Any, Literal

from app.tasks.scraper import extract_business_identity_signals, extract_maps_url_from_content


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
            if name:
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
            if country and (city or region or postal):
                display = ", ".join(value for value in (city, region, country) if value)
                markets.append(MarketSignal(display, country, region, city, postal, "json_ld", "high"))

        area_served = record.get("areaServed") or record.get("serviceArea")
        if area_served:
            has_service_area = True

        for value in _service_values(record):
            if value.casefold() not in _GENERIC_SERVICES and len(value) <= 200:
                services.append(TextSignal(value, "json_ld", "high"))

    for signal in extract_business_identity_signals(html, scope="preflight_homepage"):
        confidence: Confidence = "high" if signal.quality == "strong" else "medium" if signal.quality == "supporting" else "low"
        candidate = TextSignal(signal.value, signal.source, confidence)
        if signal.field == "name":
            names.append(candidate)
        elif signal.field == "phone":
            phones.append(candidate)

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
        gbp_url=extract_maps_url_from_content(html),
    )
