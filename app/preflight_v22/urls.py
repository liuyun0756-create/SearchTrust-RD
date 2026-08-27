"""URL normalization and SSRF boundaries for v2.2 preflight."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import SplitResult, urlsplit, urlunsplit


Resolver = Callable[[str], Awaitable[list[str]]]

_BLOCKED_HOSTNAMES = {
    "localhost",
    "metadata.google",
    "metadata.google.internal",
}
_ALLOWED_PORTS = {80, 443}


class UrlSafetyError(ValueError):
    """The supplied URL is not safe for a server-side request."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.user_message = message


class UrlUnreachableError(OSError):
    """A syntactically safe public target cannot currently be resolved."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.user_message = message


@dataclass(frozen=True)
class SafeUrl:
    normalized_url: str
    scheme: str
    hostname: str
    port: int
    addresses: tuple[str, ...]


@dataclass(frozen=True)
class _ParsedUrl:
    split: SplitResult
    scheme: str
    hostname: str
    port: int
    explicit_port: bool


def _parse_url(value: str) -> _ParsedUrl:
    raw = str(value).strip()
    try:
        parsed = urlsplit(raw)
    except ValueError as exc:
        raise UrlSafetyError("URL_INVALID", "The URL is invalid.") from exc

    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise UrlSafetyError(
            "URL_SCHEME_UNSUPPORTED",
            "Only HTTP and HTTPS URLs are supported.",
        )
    if parsed.username is not None or parsed.password is not None:
        raise UrlSafetyError(
            "URL_USERINFO_FORBIDDEN",
            "URLs containing user information are not allowed.",
        )
    hostname = (parsed.hostname or "").strip().rstrip(".")
    if not hostname:
        raise UrlSafetyError("URL_HOST_MISSING", "The URL must include a hostname.")
    try:
        hostname = hostname.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise UrlSafetyError("URL_HOST_INVALID", "The URL hostname is invalid.") from exc
    if hostname in _BLOCKED_HOSTNAMES or hostname.endswith(".localhost"):
        raise UrlSafetyError(
            "URL_HOST_FORBIDDEN",
            "The URL hostname is not a public target.",
        )
    try:
        parsed_port = parsed.port
    except ValueError as exc:
        raise UrlSafetyError("URL_PORT_INVALID", "The URL port is invalid.") from exc
    explicit_port = parsed_port is not None
    port = parsed_port or (443 if scheme == "https" else 80)
    if explicit_port and port not in _ALLOWED_PORTS:
        raise UrlSafetyError(
            "URL_PORT_FORBIDDEN",
            "Only public web ports 80 and 443 are allowed.",
        )

    try:
        direct_address = ipaddress.ip_address(hostname)
    except ValueError:
        direct_address = None
    if direct_address is not None:
        _require_global_address(direct_address)

    return _ParsedUrl(
        split=parsed,
        scheme=scheme,
        hostname=hostname,
        port=port,
        explicit_port=explicit_port,
    )


def _authority(parsed: _ParsedUrl) -> str:
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    default_port = 443 if parsed.scheme == "https" else 80
    if parsed.explicit_port and parsed.port != default_port:
        return f"{host}:{parsed.port}"
    return host


def normalize_site_url(value: str) -> str:
    """Return the canonical root identity for a safe URL syntax."""

    parsed = _parse_url(value)
    return urlunsplit((parsed.scheme, _authority(parsed), "/", "", ""))


def _require_global_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> None:
    if not address.is_global:
        raise UrlSafetyError(
            "URL_ADDRESS_FORBIDDEN",
            "The URL resolves to a private or reserved address.",
        )


async def _system_resolver(hostname: str) -> list[str]:
    def lookup() -> list[str]:
        records = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
        return [str(record[4][0]) for record in records]

    return await asyncio.to_thread(lookup)


async def resolve_public_url(value: str, *, resolver: Resolver | None = None) -> SafeUrl:
    """Resolve a URL once and return only a fully checked public target."""

    parsed = _parse_url(value)
    try:
        direct_address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        direct_address = None

    if direct_address is not None:
        address_values = [str(direct_address)]
    else:
        try:
            address_values = await (resolver or _system_resolver)(parsed.hostname)
        except socket.gaierror as exc:
            raise UrlUnreachableError(
                "SITE_DNS_UNAVAILABLE",
                "The site hostname could not be resolved.",
            ) from exc
        if not address_values:
            raise UrlUnreachableError(
                "SITE_DNS_UNAVAILABLE",
                "The site hostname could not be resolved.",
            )

    unique_addresses: list[str] = []
    for raw_address in address_values:
        try:
            address = ipaddress.ip_address(raw_address)
        except ValueError as exc:
            raise UrlSafetyError(
                "URL_ADDRESS_INVALID",
                "The site hostname resolved to an invalid address.",
            ) from exc
        _require_global_address(address)
        canonical = str(address)
        if canonical not in unique_addresses:
            unique_addresses.append(canonical)

    return SafeUrl(
        normalized_url=urlunsplit((parsed.scheme, _authority(parsed), "/", "", "")),
        scheme=parsed.scheme,
        hostname=parsed.hostname,
        port=parsed.port,
        addresses=tuple(unique_addresses),
    )


def validate_gbp_url(value: str) -> str:
    """Accept only known Google Maps and public GBP URL shapes."""

    raw = str(value).strip()
    parsed = _parse_url(raw)
    path = parsed.split.path or "/"
    host = parsed.hostname
    supported = (
        (host in {"google.com", "www.google.com"} and path.startswith("/maps"))
        or host == "maps.google.com"
        or (host == "search.google.com" and path.startswith("/local/"))
        or (host == "maps.app.goo.gl" and path != "/")
        or (host == "goo.gl" and path.startswith("/maps/"))
        or (host == "share.google" and path != "/")
    )
    if not supported:
        raise UrlSafetyError(
            "GBP_URL_UNSUPPORTED",
            "The GBP URL must be a supported public Google Maps URL.",
        )
    return raw
