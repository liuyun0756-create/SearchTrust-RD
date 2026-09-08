"""Conservative Case-host page identity and same-source consolidation."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Generic, Iterable, TypeVar
from urllib.parse import unquote, urlsplit, urlunsplit


_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_BAD_PERCENT = re.compile(r"%(?![0-9A-Fa-f]{2})")
_UNRESERVED_ESCAPE = re.compile(r"%([0-9A-Fa-f]{2})")
_UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
_TRACKING = frozenset({"gclid", "dclid", "fbclid", "gbraid", "wbraid"})
T = TypeVar("T")


def _host(value: str) -> str:
    host = value.rstrip(".").lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def _decode_unreserved(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        char = chr(int(match.group(1), 16))
        return char if char in _UNRESERVED else f"%{match.group(1).upper()}"
    return _UNRESERVED_ESCAPE.sub(replace, value)


def _query(value: str) -> str | None:
    if _BAD_PERCENT.search(value) or _CONTROL.search(value) or "\\" in value:
        return None
    retained: list[tuple[str, str, bool]] = []
    if not value:
        return ""
    for component in value.split("&"):
        key, separator, raw_value = component.partition("=")
        try:
            decoded_key = unquote(key, errors="strict")
        except (UnicodeDecodeError, ValueError):
            return None
        folded = decoded_key.casefold()
        if folded.startswith("utm_") or folded in _TRACKING:
            continue
        retained.append((_decode_unreserved(key), _decode_unreserved(raw_value), bool(separator)))
    retained.sort(key=lambda item: (item[0], item[1], item[2]))
    return "&".join(f"{key}={value}" if had_equals else key for key, value, had_equals in retained)


def _path(value: str) -> str | None:
    if not value.startswith("/") or _BAD_PERCENT.search(value) or _CONTROL.search(value) or "\\" in value:
        return None
    normalized = _decode_unreserved(value)
    if any(segment in {".", ".."} for segment in normalized.split("/")):
        return None
    if normalized != "/":
        normalized = normalized.rstrip("/") or "/"
    return normalized


def normalize_gsc_page(value: str, normalized_domain: str) -> str | None:
    if not isinstance(value, str) or len(value) > 4096 or _CONTROL.search(value) or "\\" in value:
        return None
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        return None
    default_port = (parsed.scheme == "http" and port == 80) or (parsed.scheme == "https" and port == 443)
    if _host(parsed.hostname) != _host(normalized_domain) or (port is not None and not default_port):
        return None
    path = _path(parsed.path or "/")
    query = _query(parsed.query)
    if path is None or query is None:
        return None
    return urlunsplit(("https", _host(normalized_domain), path, query, ""))


def normalize_ga4_page(value: str, normalized_domain: str) -> str | None:
    if not isinstance(value, str) or len(value) > 4096 or value == "(not set)":
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme or parsed.netloc:
        return None
    path = _path(parsed.path)
    query = _query(parsed.query)
    if path is None or query is None:
        return None
    return urlunsplit(("https", _host(normalized_domain), path, query, ""))


@dataclass(frozen=True)
class ConsolidatedPage(Generic[T]):
    canonical_url: str
    rows: tuple[T, ...]
    raw_keys: tuple[str, ...]


def consolidate_pages(
    rows: Iterable[T], *, key, normalizer, normalized_domain: str,
) -> tuple[dict[str, ConsolidatedPage[T]], tuple[str, ...]]:
    grouped: dict[str, list[tuple[str, T]]] = {}
    rejected: list[str] = []
    for row in rows:
        raw = key(row)
        canonical = normalizer(raw, normalized_domain)
        if canonical is None:
            rejected.append(raw)
            continue
        grouped.setdefault(canonical, []).append((raw, row))
    return ({canonical: ConsolidatedPage(
        canonical_url=canonical,
        rows=tuple(row for _, row in sorted(values, key=lambda item: item[0])),
        raw_keys=tuple(raw for raw, _ in sorted(values, key=lambda item: item[0])),
    ) for canonical, values in sorted(grouped.items())}, tuple(sorted(set(rejected))))
