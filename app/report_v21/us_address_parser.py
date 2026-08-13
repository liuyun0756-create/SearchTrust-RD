"""US address component parsing behind a stable internal interface."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

try:  # Fail-closed behavior is handled by address_facts during rolling deploys.
    import usaddress
except ImportError:  # pragma: no cover - deployment installs requirements.txt
    usaddress = None  # type: ignore[assignment]


US_STATE_CODES: frozenset[str] = frozenset({
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI",
    "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI",
    "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC",
    "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT",
    "VT", "VA", "WA", "WV", "WI", "WY", "DC", "AS", "GU", "MP", "PR",
    "VI",
})

_STREET_LABELS = (
    "StreetNamePreModifier",
    "StreetNamePreType",
    "StreetNamePreDirectional",
    "StreetName",
    "StreetNamePostType",
    "StreetNamePostDirectional",
    "StreetNamePostModifier",
)


@dataclass(frozen=True)
class ParsedAddress:
    raw_value: str
    status: str
    address_type: str = ""
    components: dict[str, str] = field(default_factory=dict)
    rejection_reason: str | None = None
    parser: str = "usaddress"


def parser_available() -> bool:
    return usaddress is not None


def parse_us_address(raw_value: str) -> ParsedAddress:
    """Parse a candidate without deciding whether it is a valid page fact."""
    raw = " ".join(str(raw_value or "").split()).strip()
    if not raw:
        return ParsedAddress(raw, "rejected", rejection_reason="empty_value")
    if usaddress is None:
        return ParsedAddress(raw, "unavailable", rejection_reason="parser_unavailable")

    try:
        tagged, address_type = usaddress.tag(raw)
    except Exception as exc:  # RepeatedLabelError subclasses a parser-specific error.
        return ParsedAddress(
            raw,
            "ambiguous",
            rejection_reason="ambiguous_parse",
            components={"parser_error": type(exc).__name__},
        )

    values = {str(key): _clean(value) for key, value in tagged.items() if _clean(value)}
    components = {
        "house_number": values.get("AddressNumber", ""),
        "street": " ".join(values[label] for label in _STREET_LABELS if values.get(label)),
        "street_name": values.get("StreetName", ""),
        "street_suffix": values.get("StreetNamePostType", ""),
        "unit": " ".join(
            value
            for value in (values.get("OccupancyType", ""), values.get("OccupancyIdentifier", ""))
            if value
        ),
        "city": values.get("PlaceName", ""),
        "state": values.get("StateName", "").upper(),
        "postal_code": values.get("ZipCode", ""),
        "country": values.get("CountryName", ""),
    }
    unsupported = {
        key: value
        for key, value in values.items()
        if key in {"Recipient", "NotAddress", "USPSBoxType", "USPSBoxID"}
    }
    if unsupported:
        components["unsupported"] = " | ".join(unsupported.values())

    normalized_type = str(address_type or "").strip()
    return ParsedAddress(
        raw,
        "parsed",
        address_type=normalized_type,
        components={key: value for key, value in components.items() if value},
    )


def _clean(value: Any) -> str:
    return " ".join(str(value or "").replace(",", " ").split()).strip()
