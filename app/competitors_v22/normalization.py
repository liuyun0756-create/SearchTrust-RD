"""Deterministic identity and relevance normalization for competitor records."""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import urlsplit


_NON_WORD = re.compile(r"[^\w]+", re.UNICODE)
_BUSINESS_SUFFIXES = {
    "co",
    "company",
    "corp",
    "corporation",
    "inc",
    "incorporated",
    "limited",
    "llc",
    "llp",
    "ltd",
    "pllc",
}
_RELEVANCE_STOPWORDS = {
    "a",
    "an",
    "and",
    "at",
    "best",
    "for",
    "in",
    "local",
    "me",
    "near",
    "of",
    "the",
    "to",
    "top",
}
_TOKEN_ALIASES = {
    "plumber": "plumb",
    "plumbers": "plumb",
    "plumbing": "plumb",
}


def normalize_text(value: str | None) -> str:
    if not value:
        return ""
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(_NON_WORD.sub(" ", normalized).split())


def normalize_name(value: str | None) -> str:
    tokens = normalize_text(value).split()
    while tokens and tokens[-1] in _BUSINESS_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def normalize_address(value: str | None) -> str:
    return normalize_text(value)


def normalize_domain(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urlsplit(value.strip())
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        return None
    host = parsed.hostname.casefold().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        return None


def canonical_competitor_url(value: str | None) -> str | None:
    domain = normalize_domain(value)
    return None if domain is None else f"https://{domain}/"


def tokenize_relevance_text(value: str | None) -> set[str]:
    tokens: set[str] = set()
    for token in normalize_text(value).split():
        if token in _RELEVANCE_STOPWORDS:
            continue
        tokens.add(_TOKEN_ALIASES.get(token, token))
    return tokens
