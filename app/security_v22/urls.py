"""Shared V2.2 URL normalization, resolution and SSRF policy.

The implementation remains re-exported from its original preflight module so older
imports stay compatible while every V2.2 network consumer can depend on this stable
security boundary.
"""

from app.preflight_v22.urls import (
    Resolver,
    SafeUrl,
    UrlSafetyError,
    UrlUnreachableError,
    normalize_site_url,
    resolve_public_url,
    validate_gbp_url,
)

__all__ = [
    "Resolver",
    "SafeUrl",
    "UrlSafetyError",
    "UrlUnreachableError",
    "normalize_site_url",
    "resolve_public_url",
    "validate_gbp_url",
]
