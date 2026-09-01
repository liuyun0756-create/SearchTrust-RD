"""Versioned, offline and deliberately conservative site/GBP comparators."""
import json
import re
import unicodedata
from collections.abc import Mapping, Sequence

import phonenumbers
import usaddress

from app.report_v22.site_gbp_alignment_models import (
    NormalizedValue,
    ServiceAreaComparison,
    ValueComparison,
)

NAME_VERSION = "business_name_normalization_v1"
PHONE_VERSION = "business_phone_normalization_v1"
ADDRESS_VERSION = "business_address_normalization_v1"
SERVICE_AREA_VERSION = "service_area_normalization_v1"

_SAFE_PUNCTUATION = re.compile(r"[\.,;:!?'\"`()\[\]{}\-_/\\]+")
_WHITESPACE = re.compile(r"\s+")
_US_LEGAL_SUFFIXES = {
    ("llc",), ("l", "l", "c"), ("inc",), ("incorporated",),
    ("corp",), ("corporation",), ("co",), ("company",),
    ("lp",), ("llp",), ("pllc",),
}
_LEGAL_SUFFIXES = {"US": _US_LEGAL_SUFFIXES}
_STREET_SUFFIX = {
    "street": "st", "st": "st", "road": "rd", "rd": "rd",
    "avenue": "ave", "ave": "ave", "boulevard": "blvd", "blvd": "blvd",
    "drive": "dr", "dr": "dr", "lane": "ln", "ln": "ln",
    "court": "ct", "ct": "ct", "circle": "cir", "cir": "cir",
    "highway": "hwy", "hwy": "hwy", "parkway": "pkwy", "pkwy": "pkwy",
    "place": "pl", "pl": "pl", "terrace": "ter", "ter": "ter",
    "trail": "trl", "trl": "trl", "way": "way",
}
_DIRECTION = {
    "north": "n", "south": "s", "east": "e", "west": "w",
    "northeast": "ne", "northwest": "nw", "southeast": "se", "southwest": "sw",
    "n": "n", "s": "s", "e": "e", "w": "w", "ne": "ne", "nw": "nw", "se": "se", "sw": "sw",
}
_US_REGIONS = {
    "alabama": "al", "alaska": "ak", "arizona": "az", "arkansas": "ar",
    "california": "ca", "colorado": "co", "connecticut": "ct", "delaware": "de",
    "florida": "fl", "georgia": "ga", "hawaii": "hi", "idaho": "id",
    "illinois": "il", "indiana": "in", "iowa": "ia", "kansas": "ks",
    "kentucky": "ky", "louisiana": "la", "maine": "me", "maryland": "md",
    "massachusetts": "ma", "michigan": "mi", "minnesota": "mn", "mississippi": "ms",
    "missouri": "mo", "montana": "mt", "nebraska": "ne", "nevada": "nv",
    "new hampshire": "nh", "new jersey": "nj", "new mexico": "nm", "new york": "ny",
    "north carolina": "nc", "north dakota": "nd", "ohio": "oh", "oklahoma": "ok",
    "oregon": "or", "pennsylvania": "pa", "rhode island": "ri", "south carolina": "sc",
    "south dakota": "sd", "tennessee": "tn", "texas": "tx", "utah": "ut",
    "vermont": "vt", "virginia": "va", "washington": "wa", "west virginia": "wv",
    "wisconsin": "wi", "wyoming": "wy", "district of columbia": "dc",
}


def target_region(display_name: str, region: str | None, country_code: str) -> str | None:
    if region:
        return region
    if country_code == "US":
        match = re.search(r"(?:,|\s)\s*([A-Za-z]{2})\s*$", display_name)
        if match:
            return match.group(1).upper()
    return None


def _display(value: str) -> str:
    return _WHITESPACE.sub(" ", unicodedata.normalize("NFKC", value).strip())


def _words(value: str) -> str:
    return _WHITESPACE.sub(" ", _SAFE_PUNCTUATION.sub(" ", _display(value).casefold())).strip()


def normalize_business_name(value: str, country_code: str) -> NormalizedValue:
    original = _display(value)
    transformed = _words(original)
    changes: list[str] = []
    if original != value:
        changes.append("unicode_or_whitespace")
    if "&" in transformed or re.search(r"\band\b", transformed):
        replaced = re.sub(r"\band\b", "and", transformed.replace("&", " and "))
        transformed = _WHITESPACE.sub(" ", replaced).strip()
        changes.append("and_equivalent")
    tokens = transformed.split()
    suffixes = _LEGAL_SUFFIXES.get(country_code, set())
    for suffix in sorted(suffixes, key=len, reverse=True):
        if tuple(tokens[-len(suffix):]) == suffix:
            tokens = tokens[:-len(suffix)]
            changes.append("legal_suffix_removed")
            break
    key = " ".join(tokens)
    return NormalizedValue(field="business_name", original_key=original, normalized_key=key or None,
                           version=NAME_VERSION, country_code=country_code,
                           transformations=changes, valid=bool(key))


def compare_business_names(left: str, right: str, country_code: str) -> ValueComparison:
    a, b = normalize_business_name(left, country_code), normalize_business_name(right, country_code)
    if not a.valid or not b.valid:
        state = "incomparable"
    elif a.original_key == b.original_key:
        state = "exact_match"
    elif a.normalized_key == b.normalized_key:
        state = "semantic_match"
    else:
        state = "mismatch"
    return ValueComparison(state=state, left=a, right=b)


def normalize_phone(value: str, country_code: str) -> NormalizedValue:
    original = _display(value)
    changes: list[str] = []
    try:
        number = phonenumbers.parse(original, None if original.startswith("+") else country_code)
        valid = phonenumbers.is_possible_number(number) and phonenumbers.is_valid_number(number)
    except phonenumbers.NumberParseException:
        number, valid = None, False
    if not valid or number is None:
        return NormalizedValue(field="phone", original_key=original, normalized_key=None,
                               version=PHONE_VERSION, country_code=country_code,
                               transformations=["invalid_phone"], valid=False)
    key = phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164)
    extension = number.extension or None
    if original != key:
        changes.append("phone_format")
    if extension:
        changes.append("extension_observed")
    return NormalizedValue(field="phone", original_key=original, normalized_key=key,
                           extension=extension, version=PHONE_VERSION,
                           country_code=country_code, transformations=changes)


def compare_phones(left: str, right: str, country_code: str) -> ValueComparison:
    a, b = normalize_phone(left, country_code), normalize_phone(right, country_code)
    if not a.valid or not b.valid:
        state = "incomparable"
    elif a.normalized_key != b.normalized_key:
        state = "mismatch"
    elif a.extension and b.extension and a.extension != b.extension:
        state = "mismatch"
    elif a.original_key == b.original_key:
        state = "exact_match"
    else:
        state = "semantic_match"
    omitted = ["extension"] if a.valid and b.valid and bool(a.extension) != bool(b.extension) else []
    return ValueComparison(state=state, left=a, right=b, compared_components=["number"], omitted_components=omitted)


def _component(value: str) -> str:
    return _words(value)


def _street_token(value: str) -> str:
    token = _component(value)
    return _STREET_SUFFIX.get(token, _DIRECTION.get(token, token))


def _tag_us(value: str) -> dict[str, str]:
    try:
        tagged, address_type = usaddress.tag(value)
    except (usaddress.RepeatedLabelError, TypeError):
        return {}
    if address_type != "Street Address":
        return {}
    street_parts = []
    for label in ("StreetNamePreDirectional", "StreetName", "StreetNamePostType", "StreetNamePostDirectional"):
        if tagged.get(label):
            street_parts.extend(_street_token(token) for token in tagged[label].split())
    region = _component(tagged.get("StateName", ""))
    region = _US_REGIONS.get(region, region)
    components = {
        "house": _component(tagged.get("AddressNumber", "")),
        "street": " ".join(street_parts),
        "locality": _component(tagged.get("PlaceName", "")),
        "region": region,
        "postal": re.sub(r"\s+", "", _component(tagged.get("ZipCode", ""))),
        "unit": _component(" ".join(filter(None, [tagged.get("OccupancyType"), tagged.get("OccupancyIdentifier")]))),
        "country": "us",
    }
    return {key: item for key, item in components.items() if item}


def _structured_address(value: Mapping[str, str], country_code: str) -> dict[str, str]:
    street = str(value.get("streetAddress", ""))
    if country_code == "US":
        components = _tag_us(street)
    else:
        match = re.fullmatch(r"\s*(\d+[\w-]*)\s+(.+?)\s*", street)
        components = {"house": _component(match.group(1)), "street": _component(match.group(2))} if match else {}
    mapped = {
        "locality": value.get("addressLocality"), "region": value.get("addressRegion"),
        "postal": value.get("postalCode"), "country": value.get("addressCountry"),
    }
    for key, item in mapped.items():
        if item:
            normalized = _component(str(item))
            if key == "region" and country_code == "US":
                normalized = _US_REGIONS.get(normalized, normalized)
            if key == "postal":
                normalized = normalized.replace(" ", "")
            components[key] = normalized
    if country_code == "US" and "country" not in components:
        components["country"] = "us"
    return components


def normalize_address(value: str | Mapping[str, str], country_code: str) -> NormalizedValue:
    structured = isinstance(value, Mapping)
    original = (json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                if structured else _display(str(value)))
    if structured:
        components = _structured_address(value, country_code)
    elif country_code == "US":
        components = _tag_us(str(value))
    else:
        components = {}
    valid = bool(components.get("house") and components.get("street"))
    key = "|".join(f"{name}={components[name]}" for name in sorted(components)) if valid else None
    changes = ["structured_components"] if structured else []
    if valid and not structured and _words(str(value)) != _words(original):
        changes.append("unicode_or_whitespace")
    return NormalizedValue(field="address", original_key=original, normalized_key=key,
                           components=components, version=ADDRESS_VERSION,
                           country_code=country_code, transformations=changes,
                           valid=valid)


def compare_addresses(left: str | Mapping[str, str], right: str | Mapping[str, str], country_code: str) -> ValueComparison:
    a, b = normalize_address(left, country_code), normalize_address(right, country_code)
    if not a.valid or not b.valid:
        return ValueComparison(state="incomparable", left=a, right=b)
    compared = sorted(set(a.components) & set(b.components))
    omitted = sorted(set(a.components) ^ set(b.components))
    if any(a.components[key] != b.components[key] for key in ("house", "street")):
        state = "mismatch"
    elif any(a.components[key] != b.components[key] for key in compared if key not in {"house", "street"}):
        state = "mismatch"
    elif a.original_key == b.original_key:
        state = "exact_match"
    else:
        state = "semantic_match"
    return ValueComparison(state=state, left=a, right=b,
                           compared_components=compared, omitted_components=omitted)


def _service_key(value: str, country_code: str, region: str | None) -> str:
    key = _words(value)
    suffixes = {_component(country_code), _component(region or "")}
    if country_code == "US":
        suffixes.update({"us", "usa", "united states", _US_REGIONS.get(_component(region or ""), "")})
    suffixes.discard("")
    changed = True
    while changed:
        changed = False
        for suffix in sorted(suffixes, key=len, reverse=True):
            if key.endswith(" " + suffix):
                key = key[:-(len(suffix) + 1)].strip()
                changed = True
                break
    return key


def normalize_service_area(value: str, country_code: str, *, region: str | None = None) -> NormalizedValue:
    original = _display(value)
    key = _service_key(value, country_code, region)
    transformations = ["location_suffix_removed"] if key != _words(value) else []
    return NormalizedValue(field="service_area", original_key=original, normalized_key=key or None,
                           version=SERVICE_AREA_VERSION, country_code=country_code,
                           transformations=transformations, valid=bool(key))


def compare_service_areas(site_values: Sequence[str], gbp_values: Sequence[str], country_code: str,
                          *, region: str | None = None) -> ServiceAreaComparison:
    site = {_service_key(value, country_code, region) for value in site_values}
    gbp = {_service_key(value, country_code, region) for value in gbp_values}
    site.discard("")
    gbp.discard("")
    matched = site & gbp
    state = "exact_match" if site == gbp else "partial_match" if matched else "mismatch"
    return ServiceAreaComparison(
        state=state,
        matched=sorted(matched),
        site_only=sorted(site - gbp),
        gbp_only=sorted(gbp - site),
    )
