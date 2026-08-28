"""Pure URL scope, normalization, filtering, and digest helpers."""

from __future__ import annotations

import hashlib
import ipaddress
import posixpath
import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from app.preflight_v22.urls import UrlSafetyError, normalize_site_url


_TRACKING_QUERY_PREFIXES = ("utm_",)
_TRACKING_QUERY_KEYS = {
    "fbclid",
    "gclid",
    "gbraid",
    "mc_cid",
    "mc_eid",
    "msclkid",
    "ref",
    "source",
    "wbraid",
}
_REJECTED_QUERY_KEYS = {
    "filter",
    "facet",
    "order",
    "page",
    "paged",
    "q",
    "query",
    "s",
    "search",
    "session",
    "sessionid",
    "sid",
    "sort",
}
_REJECTED_PATH_SEGMENTS = {
    "account",
    "admin",
    "cart",
    "checkout",
    "delete",
    "login",
    "logout",
    "my-account",
    "register",
    "search",
    "signin",
    "signup",
    "wp-admin",
}
_STATIC_EXTENSIONS = {
    ".7z",
    ".avi",
    ".bmp",
    ".css",
    ".csv",
    ".doc",
    ".docx",
    ".eot",
    ".exe",
    ".gif",
    ".gz",
    ".ico",
    ".jpeg",
    ".jpg",
    ".js",
    ".json",
    ".m4a",
    ".mov",
    ".mp3",
    ".mp4",
    ".ogg",
    ".pdf",
    ".png",
    ".ppt",
    ".pptx",
    ".rar",
    ".rss",
    ".svg",
    ".tar",
    ".tif",
    ".tiff",
    ".ttf",
    ".txt",
    ".wav",
    ".webm",
    ".webp",
    ".woff",
    ".woff2",
    ".xls",
    ".xlsx",
    ".xml",
    ".zip",
}
_DEFAULT_DOCUMENTS = {"index.htm", "index.html", "index.php", "default.htm", "default.html"}


@dataclass(frozen=True)
class SiteScope:
    root_url: str
    scheme: str
    canonical_host: str
    allowed_hosts: frozenset[str]

    @classmethod
    def from_root(cls, root_url: str) -> "SiteScope":
        normalized = normalize_site_url(root_url)
        parsed = urlsplit(normalized)
        canonical_host = parsed.hostname or ""
        allowed = {canonical_host}
        try:
            ipaddress.ip_address(canonical_host)
        except ValueError:
            if canonical_host.startswith("www."):
                allowed.add(canonical_host[4:])
            else:
                allowed.add(f"www.{canonical_host}")
        return cls(
            root_url=normalized,
            scheme=parsed.scheme,
            canonical_host=canonical_host,
            allowed_hosts=frozenset(allowed),
        )

    def contains_host(self, hostname: str) -> bool:
        try:
            canonical = hostname.strip().rstrip(".").encode("idna").decode("ascii").lower()
        except UnicodeError:
            return False
        return canonical in self.allowed_hosts


def _normalized_path(path: str) -> str:
    collapsed = re.sub(r"/{2,}", "/", path or "/")
    normalized = posixpath.normpath(collapsed)
    if not normalized.startswith("/"):
        normalized = f"/{normalized}"
    if normalized == "/.":
        normalized = "/"
    segments = normalized.rstrip("/").split("/")
    if segments and segments[-1].casefold() in _DEFAULT_DOCUMENTS:
        normalized = "/".join(segments[:-1]) or "/"
    if normalized != "/":
        normalized = normalized.rstrip("/")
    return normalized


def _path_is_excluded(path: str) -> bool:
    segments = [segment.casefold() for segment in path.split("/") if segment]
    if any(segment in _REJECTED_PATH_SEGMENTS for segment in segments):
        return True
    if any(token in {"remove", "destroy", "unsubscribe"} for token in segments):
        return True
    final_segment = segments[-1] if segments else ""
    return any(final_segment.endswith(extension) for extension in _STATIC_EXTENSIONS)


def canonicalize_inventory_url(
    value: str,
    *,
    scope: SiteScope,
    base_url: str | None = None,
) -> str | None:
    """Return a stable in-scope page URL, or ``None`` when it is excluded."""

    raw = str(value).strip()
    if not raw:
        return None
    try:
        absolute = urljoin(base_url or scope.root_url, raw)
        parsed = urlsplit(absolute)
    except ValueError:
        return None
    if parsed.scheme.casefold() not in {"http", "https"}:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    hostname = (parsed.hostname or "").strip().rstrip(".")
    if not scope.contains_host(hostname):
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    if port is not None and port not in {80, 443}:
        return None

    path = _normalized_path(parsed.path)
    if _path_is_excluded(path):
        return None

    query_items: list[tuple[str, str]] = []
    try:
        raw_query_items = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=False)
    except ValueError:
        return None
    for raw_key, raw_value in raw_query_items:
        key = raw_key.strip()
        lowered = key.casefold()
        if lowered in _REJECTED_QUERY_KEYS:
            return None
        if lowered in _TRACKING_QUERY_KEYS or lowered.startswith(_TRACKING_QUERY_PREFIXES):
            continue
        query_items.append((key, raw_value))
    query_items.sort(key=lambda item: (item[0].casefold(), item[1]))

    return urlunsplit(
        (
            scope.scheme,
            scope.canonical_host,
            path,
            urlencode(query_items, doseq=True),
            "",
        )
    )


def canonicalize_site_resource_url(
    value: str,
    *,
    scope: SiteScope,
    base_url: str | None = None,
) -> str | None:
    """Normalize an in-scope support resource such as robots or sitemap XML."""

    raw = str(value).strip()
    if not raw:
        return None
    try:
        parsed = urlsplit(urljoin(base_url or scope.root_url, raw))
    except ValueError:
        return None
    if parsed.scheme.casefold() not in {"http", "https"}:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    hostname = (parsed.hostname or "").strip().rstrip(".")
    if not scope.contains_host(hostname):
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    if port is not None and port not in {80, 443}:
        return None
    path = _normalized_path(parsed.path)
    return urlunsplit((scope.scheme, scope.canonical_host, path, parsed.query, ""))


def stable_url_digest(url: str) -> str:
    return f"sha256:{hashlib.sha256(url.encode('utf-8')).hexdigest()}"


def require_scope(root_url: str) -> SiteScope:
    """Build a scope while preserving the shared preflight syntax errors."""

    try:
        return SiteScope.from_root(root_url)
    except UrlSafetyError:
        raise
