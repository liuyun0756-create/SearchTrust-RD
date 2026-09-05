"""Canonical HMAC signing for Vercel's short-lived Google token broker."""

from __future__ import annotations

import hashlib
import hmac

GOOGLE_BROKER_SIGNATURE_VERSION = "v1"


def sign_google_broker_body(
    secret: str,
    *,
    timestamp: int,
    request_id: str,
    nonce: str,
    body: str,
    version: str = GOOGLE_BROKER_SIGNATURE_VERSION,
) -> str:
    canonical = f"{version}.{timestamp}.{request_id}.{nonce}.{body}"
    digest = hmac.new(secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"sha256={digest}"
